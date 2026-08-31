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

    重要限制（2026-08-31 代码审查发现，已用真实 Redis 独立验证）：
    fakeredis 2.37.1 的 WATCH 不会检测“另一个连接改了被 watch 的 key”，也就是说
    release/续期里 WATCH/MULTI/EXEC 对“并发覆盖”的保护，在 fakeredis 单测里
    测不出来——同一段代码在真实 Redis 上 EXEC 会正确抛 WatchError 阻止误删，
    在 fakeredis 上却会静默成功。因此涉及“并发覆盖会被 WatchError 挡住”这条
    结论，测试只能覆盖“比较值”这一步本身的正确性（串行注入一个不匹配的值），
    无法在 fakeredis 上覆盖真正的并发竞态本身；后者已经用真实 Redis 单独跑过
    对比实验确认可靠。

    TTL 与续期：不再用一个能覆盖“单次浏览器任务最坏总耗时”的超长 TTL
    （旧值 600s 仍然不够——仅验证码远程求解一步的最低预留就是 300s，见
    `common/services/captcha/remote_timeout.py` 的 `_MIN_REMOTE_SOLVE_TIMEOUT_SECONDS`，
    加上登录/发布流程本身的等待，真实任务超过 600s 是合理可能的；一旦 TTL 到期
    而任务还没跑完，slot 会被 Redis 自动删掉，另一个进程立刻抢到同一 slot 索引
    并启动新的 Chromium，而原来的 Chromium 其实还在运行——变成 1 个 slot 名额
    下同时存在 2 个真实浏览器进程，正是这个限流器要防的 OOM 场景，而且原持有者
    之后 release 时会因为 value 不匹配而静默 no-op，现场没有任何告警）。
    改为“持有期间定期续期 TTL”：`browser_slot` 在后台跑一个心跳任务，每隔
    `_RENEW_INTERVAL_SECONDS` 用 WATCH/MULTI/EXEC 给自己的 slot 续期一次
    `_SLOT_TTL_SECONDS`（仅当 value 仍是自己的才续期）。这样 TTL 不再需要覆盖
    任务总时长（心跳只要活着就会不断把 TTL 往后推，任务再长也不会因为超时被
    误回收），只需要覆盖“连续错过几次心跳还没恢复”这一更短的窗口，因此可以把
    TTL 定得更小，换来进程真的崩溃后更快地把 slot 收回来——TTL 越大，崩溃后
    slot 空占的时间就越长，那段时间里业务实际上是“少了一个名额”，同样是需要
    尽量缩短的后果。这里取 `_RENEW_INTERVAL_SECONDS = 30`、`_SLOT_TTL_SECONDS = 90`
    （3 倍关系），允许连续错过 2 次心跳（例如短暂的 Redis 网络抖动）还有余量，
    第 3 次仍未续上才会被判定为泄漏并回收；相比旧的 600s，崩溃恢复延迟从最长
    10 分钟降到最长 90 秒。

    残留风险（2026-08-31 二轮复审裁决：接受，不修复，仅记录）：acquire 阶段
    （`SET NX`）是结构性不可能超额的数学事实——不依赖任何证明，因为可用 key
    的数量本身就是上限。但“持有期间不丢失所有权”不是同一级别的保证：
    `_renew_slot` 一旦发现槽位已经不属于自己，只会停止心跳、记一条
    `[浏览器槽位重叠占用风险]` 日志，**不会**去中止仍在运行的 body（也做不到——
    Playwright 的浏览器操作本身不可安全中断，真要做到需要下一个任务的 7 个
    调用点全部配合改造，成本和收益不成比例，尤其是这个场景只会在 Redis
    连续故障超过 90 秒（`_SLOT_TTL_SECONDS`）时触发，而那种时长的 Redis 故障
    通常意味着数据库队列、缓存等其他子系统也已经一起失效，浏览器超额只是
    并发故障之一）。也就是说：只要 Redis 连续不可用超过 90 秒，同一个 slot
    索引上短暂出现两个真实 Chromium 实例在理论上仍然可能，这个窗口靠 TTL
    压缩到 90 秒以内，而不是像 acquire 阶段那样被结构性杜绝。后续维护者不要
    把“测试全绿”误读成“整个限流器在任何情况下都有数学级别的不超额保证”——
    只有 acquire 阶段有，持有阶段是概率/时间窗口意义上的保证。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid

import redis.asyncio as aioredis
from redis.exceptions import WatchError
from loguru import logger

_KEY_PREFIX = "xianyu:browser:slot"
# 连续错过几次心跳还没恢复才视为泄漏；到期后由 Redis 自动回收（见模块 docstring）
_SLOT_TTL_SECONDS = 90
# 持有槽位期间的续期间隔；必须明显小于 _SLOT_TTL_SECONDS，留出容错余量
_RENEW_INTERVAL_SECONDS = 30
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
            # 审查 Finding B：不配超时的话，一次卡住的 Redis 调用会让续期心跳
            # 无限期挂起，进而在 browser_slot 退出时卡住 `await renew_task`，
            # 连带阻塞 release。取值沿用 common/db/redis_client.py 里已有的
            # 惯例（5s 读写 / 3s 连接），相对 30s 的续期间隔和 120s 的默认
            # acquire 超时都足够宽松，不会把正常的网络抖动误判成超时。
            socket_timeout=5,
            socket_connect_timeout=3,
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

    这里把两种性质不同的“没删成”分开记日志（审查 Finding 3）：
    - value 不匹配（WATCH/GET 时就已经不是自己的）：正常路径，通常是自己迟迟才
      调用 release、槽位早已经到期被别人合法重新占用，warning 即可。
    - WatchError（EXEC 时才发现 key 在 watch 期间被别的连接改过）：说明槽位在
      我们准备删除的这段时间窗口里被重叠占用过，是需要重点排查的危险信号，
      用 error 级别单独记录，方便从日志里一眼挑出来。
    """
    try:
        index_str, _, value = token.partition(":")
        key = _slot_key(int(index_str))
        r = _get_redis()
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                current = await pipe.get(key)
                pipe.multi()
                if current == value:
                    pipe.delete(key)
                await pipe.execute()
        except WatchError:
            logger.error(
                f"[浏览器槽位重叠占用] 释放 {key} 时检测到 WatchError："
                f"该 slot 在释放前后被其他进程修改过，说明这段时间里同一个 slot "
                f"索引上可能同时存在两个浏览器实例，已放弃删除（交由新占用者的 "
                f"生命周期管理）；token={token}"
            )
            return
        if current != value:
            logger.warning(
                f"释放浏览器槽位时发现槽位已不属于自己（token={token}, key={key}），"
                f"大概率是 TTL 到期后被其他进程重新占用，未执行删除"
            )
    except Exception as exc:  # 其余异常（如 Redis 连接失败）不应影响业务，TTL 会兜底回收
        logger.warning(f"释放浏览器槽位失败（{_SLOT_TTL_SECONDS}s 后自动回收）: {exc}")


async def _renew_slot(token: str) -> bool:
    """给自己持有的槽位续期 TTL；仅当 slot 当前的值仍是自己的才续期。

    返回 False 表示槽位已经不属于自己了——调用方（_renew_loop）应停止续期。
    这不一定意味着有 bug：也可能是本次续期恰好跟上一轮心跳之间隔太久、TTL
    已经到期被别人合法抢走；但如果原浏览器任务这时候还在跑，就是同一个 slot
    索引上存在两个浏览器实例的早期信号，值得记录下来。
    """
    try:
        index_str, _, value = token.partition(":")
        key = _slot_key(int(index_str))
        r = _get_redis()
        async with r.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            current = await pipe.get(key)
            if current != value:
                logger.warning(
                    f"[浏览器槽位重叠占用风险] 续期 {key} 时发现槽位已不属于自己"
                    f"（token={token}），停止续期；如果原浏览器任务仍在运行，"
                    f"说明该 slot 存在重叠占用风险，请检查任务是否超出预期时长"
                )
                return False
            pipe.multi()
            pipe.expire(key, _SLOT_TTL_SECONDS)
            await pipe.execute()
        return True
    except WatchError:
        logger.error(
            f"[浏览器槽位重叠占用] 续期 {token} 时检测到 WatchError："
            f"该 slot 在续期前后被其他进程修改过，可能存在重叠占用，停止续期"
        )
        return False
    except Exception as exc:
        # 网络类瞬时异常不代表槽位真的丢失，不因为一次失败就放弃后续续期
        logger.warning(f"续期浏览器槽位失败（网络异常等，将在下个周期重试）: {exc}")
        return True


async def _renew_loop(token: str, stop_event: asyncio.Event) -> None:
    """后台心跳：每隔 _RENEW_INTERVAL_SECONDS 续期一次，直到 stop_event 被置位
    或者发现槽位已经不属于自己（_renew_slot 返回 False）。"""
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_RENEW_INTERVAL_SECONDS)
            return  # stop_event 已置位，正常退出，不再多续一次
        except asyncio.TimeoutError:
            pass
        if not await _renew_slot(token):
            return


@contextlib.asynccontextmanager
async def browser_slot(timeout: float = 120.0):
    """async with browser_slot(): 内部创建浏览器。异常时也会释放。

    持有期间会启动一个后台心跳任务定期续期 TTL（见模块 docstring“TTL 与续期”）。
    退出时先置位 stop_event 并等待心跳任务自然结束，再释放槽位——保证不会有
    一次“迟到”的续期跟 release 并发抢同一个 key。

    审查 Finding A：`await renew_task` 必须包一层 try/except。心跳只是辅助子
    系统，它以异常（甚至 CancelledError——3.8+ 继承自 BaseException，普通
    `except Exception` 抓不住）结束，不能连带跳过下面的 `release_browser_slot`：
    否则一次成功跑完的浏览器任务会被心跳的内部异常掩盖成整体失败，槽位也会
    白白等 TTL（至多 `_SLOT_TTL_SECONDS` 秒）才被动回收，而不是立刻主动释放。
    """
    token = await acquire_browser_slot(timeout=timeout)
    stop_event = asyncio.Event()
    renew_task = asyncio.create_task(_renew_loop(token, stop_event))
    try:
        yield token
    finally:
        stop_event.set()
        try:
            await renew_task
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning(
                f"浏览器槽位心跳任务异常结束（不影响本次结果，槽位仍会正常释放）: {exc}"
            )
        await release_browser_slot(token)
