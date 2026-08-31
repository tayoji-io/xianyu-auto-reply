"""跨进程浏览器并发限流。

沙盒内存 4G，每个 Chromium 实例约 300-400M，backend-web / websocket / scheduler
三个服务进程各自会创建浏览器，进程内信号量无法协调，因此用 Redis 做全局信号量。
超额一个实例就可能触发 OOM 拖垮整个沙盒——届时用户的闲鱼账号会掉线、自动发货会中断，
所以这里的并发安全要求比一般的限流场景更高，不能容忍任何“短暂超额”的窗口。

并发安全设计（为什么没有用“先 ZADD 令牌再 ZCARD 检查，超额则 ZREM 回退重试”的
乐观方案）：
    那种写法的正确性依赖一个不算显然的论证——Redis 单线程串行执行保证每次决策
    读到的 zcard 都是决策那一刻的真实值，因此“先写后查、超额回退”最终不会让
    超过上限个令牌同时保持“已确认”状态。这个论证本身是站得住的，但它脆弱：
    一旦以后有人为了性能把“返回令牌”提前到 zadd 成功之后（而不是等第二次
    zcard 确认之后），或者 Redis 从单实例换成允许陈旧读的集群/只读副本，
    这个前提就会被破坏，而后果是 OOM 拖垮整个沙盒。这种“正确但依赖非平凡证明
    才能确认安全”的方案，不适合用在后果是 OOM 的地方。

    改用结构上不可能超额的方案：为并发上限 N 预先划出 N 个固定 slot key
    （xianyu:browser:slot:0 ... slot:N-1），获取槽位即对某个 slot key 执行
    原子的 `SET key value NX EX ttl`。SET NX 是 Redis 的单条原子命令，不存在
    “先写后查”的中间窗口；同一时刻能被 SET 成功的 slot key 数量不可能超过 N，
    这是由 key 的数量决定的，不依赖任何运行时计数比较，因此不需要证明——它
    本身就不可能超额。

    唯一的代价：SET NX 的经典搭档是“比较后删”的 Lua 脚本，用于释放时避免
    误删被别的进程重新抢到的同一 slot（例如原持有者超过 TTL 才姗姗来迟地释放，
    而此时另一个进程已经合法地重新占用了这个 slot）。但测试环境用的
    fakeredis 2.37.1 未装 lupa，不支持 EVAL/SCRIPT，会直接报
    `unknown command 'eval'`。这里改用 WATCH/MULTI/EXEC 事务做“比较后删”，
    fakeredis 原生支持，效果与 Lua 版本等价，且不引入新依赖。

    TTL 兜底：进程崩溃未释放时，slot key 到 TTL 后由 Redis 自动删除，不需要
    任何人显式跑清理逻辑——比乐观方案里手动 zremrangebyscore 更可靠，因为
    zremrangebyscore 依赖“之后还有人调用 acquire”才会触发清理，而 TTL 是
    Redis 服务端主动过期。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid

import redis.asyncio as aioredis
from loguru import logger

_KEY_PREFIX = "xianyu:browser:slot"
# 单个浏览器任务的最长存活时间，超过视为泄漏；TTL 到期后由 Redis 自动回收
_SLOT_TTL_SECONDS = 600
_POLL_INTERVAL = 0.5

_redis_client: aioredis.Redis | None = None


class BrowserSlotTimeout(RuntimeError):
    """在超时时间内未能获取浏览器槽位。"""


def _max_concurrent() -> int:
    try:
        return max(1, int(os.getenv("MAX_BROWSER_CONCURRENT", "2")))
    except ValueError:
        return 2


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB", "0")),
            decode_responses=True,
        )
    return _redis_client


def _slot_key(index: int) -> str:
    return f"{_KEY_PREFIX}:{index}"


async def acquire_browser_slot(timeout: float = 120.0) -> str:
    """占用一个浏览器槽位，返回令牌。超时抛 BrowserSlotTimeout。

    逐一对 limit 个固定 slot key 尝试原子的 SET NX EX，命中即拿到槽位——
    SET NX 本身就是原子的“查且写”，不存在乐观并发那种先写后查的竞态窗口，
    至多同时存在 limit 个被占用的 slot key。
    """
    r = _get_redis()
    limit = _max_concurrent()
    value = uuid.uuid4().hex
    deadline = time.monotonic() + timeout

    while True:
        for index in range(limit):
            key = _slot_key(index)
            acquired = await r.set(key, value, nx=True, ex=_SLOT_TTL_SECONDS)
            if acquired:
                return f"{index}:{value}"
        if time.monotonic() >= deadline:
            raise BrowserSlotTimeout(
                f"等待浏览器槽位超时（上限 {limit}，已等 {timeout}s）"
            )
        await asyncio.sleep(_POLL_INTERVAL)


async def release_browser_slot(token: str) -> None:
    """释放槽位。重复释放安全。

    用 WATCH/MULTI/EXEC 做“比较后删”：只有 slot key 当前的值仍等于自己持有的
    token 时才删除，避免误删 TTL 到期后被别的进程重新占用的同一 slot。
    """
    try:
        index_str, _, value = token.partition(":")
        key = _slot_key(int(index_str))
        r = _get_redis()
        async with r.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            current = await pipe.get(key)
            pipe.multi()
            if current == value:
                pipe.delete(key)
            await pipe.execute()
    except Exception as exc:  # 释放失败不应影响业务，TTL 会兜底回收
        logger.warning(f"释放浏览器槽位失败（{_SLOT_TTL_SECONDS}s 后自动回收）: {exc}")


@contextlib.asynccontextmanager
async def browser_slot(timeout: float = 120.0):
    """async with browser_slot(): 内部创建浏览器。异常时也会释放。"""
    token = await acquire_browser_slot(timeout=timeout)
    try:
        yield token
    finally:
        await release_browser_slot(token)
