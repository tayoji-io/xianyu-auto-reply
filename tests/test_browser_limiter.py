# tests/test_browser_limiter.py
"""跨进程浏览器并发限流器。用 fakeredis 验证，不依赖真实 Redis。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def limiter(monkeypatch):
    import fakeredis.aioredis
    from common.services import browser_limiter as mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(mod, "_get_redis", lambda: fake)
    monkeypatch.setenv("MAX_BROWSER_CONCURRENT", "2")
    return mod


@pytest.mark.anyio
async def test_allows_up_to_limit(limiter):
    t1 = await limiter.acquire_browser_slot(timeout=1)
    t2 = await limiter.acquire_browser_slot(timeout=1)
    assert t1 != t2
    await limiter.release_browser_slot(t1)
    await limiter.release_browser_slot(t2)


@pytest.mark.anyio
async def test_blocks_beyond_limit(limiter):
    t1 = await limiter.acquire_browser_slot(timeout=1)
    t2 = await limiter.acquire_browser_slot(timeout=1)
    with pytest.raises(limiter.BrowserSlotTimeout):
        await limiter.acquire_browser_slot(timeout=1)
    await limiter.release_browser_slot(t1)
    await limiter.release_browser_slot(t2)


@pytest.mark.anyio
async def test_release_frees_slot(limiter):
    t1 = await limiter.acquire_browser_slot(timeout=1)
    await limiter.acquire_browser_slot(timeout=1)
    await limiter.release_browser_slot(t1)
    t3 = await limiter.acquire_browser_slot(timeout=1)
    assert t3


@pytest.mark.anyio
async def test_context_manager_releases_on_exception(limiter):
    with pytest.raises(ValueError):
        async with limiter.browser_slot(timeout=1):
            raise ValueError("boom")
    # 槽位应已释放，可再次占满 2 个
    await limiter.acquire_browser_slot(timeout=1)
    await limiter.acquire_browser_slot(timeout=1)


@pytest.mark.anyio
async def test_never_exceeds_limit_under_concurrency(limiter, monkeypatch):
    """并发压力测试：远超上限的并发请求同时抢槽位，任意时刻活跃令牌数不得超过上限。

    这里不是依次串行调用，而是用 asyncio.gather 同时发起大量协程去抢占同一批
    slot key，并在“拿到槽位之后、释放之前”主动 await asyncio.sleep 让出控制权，
    制造 acquire/release 在事件循环里真实交叠执行的窗口——如果实现里存在
    “先写后查”式的竞态（例如两个协程都以为自己抢到了槽位），这里应该能在
    某次运行里观测到同一时刻活跃令牌数超过上限。
    """
    monkeypatch.setenv("MAX_BROWSER_CONCURRENT", "3")
    limit = 3
    concurrency = 30
    active: set[str] = set()
    max_observed = 0
    lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal max_observed
        token = await limiter.acquire_browser_slot(timeout=10)
        async with lock:
            active.add(token)
            max_observed = max(max_observed, len(active))
            assert len(active) <= limit, f"活跃令牌数 {len(active)} 超过上限 {limit}"
        # 持有槽位期间主动让出事件循环，给其他协程制造并发抢占的窗口
        await asyncio.sleep(0.01)
        async with lock:
            active.discard(token)
        await limiter.release_browser_slot(token)

    await asyncio.gather(*(worker() for _ in range(concurrency)))

    assert max_observed <= limit
    assert max_observed >= 1
    assert not active


@pytest.mark.anyio
async def test_release_does_not_delete_slot_reacquired_by_someone_else(limiter):
    """release 必须是“比较后删”，不能是无条件 delete。

    背景（2026-08-31 代码审查 Finding 1）：fakeredis 2.37.1 的 WATCH 不检测
    “另一个连接改了被 watch 的 key”，所以没法在 fakeredis 上用真正的并发去
    触发 WatchError；但“比较值再决定要不要删”这一步本身，可以用串行操作
    验证——手动把 slot key 的值改写成别人的 token，再释放原 token，断言这个
    key 没有被删掉。如果 release_browser_slot 被改成无条件 delete，这里会失败
    （已手动验证：把实现换成 `await r.delete(key)` 后，本测试会因为 key 被
    删掉而失败）。

    WATCH 本身在真正并发场景下会不会正确拦截误删，这条结论无法在 fakeredis
    上验证，已经用真实 Redis 单独跑过对比实验确认可靠（见审查结论 /
    task-4-report.md）。
    """
    t1 = await limiter.acquire_browser_slot(timeout=1)
    r = limiter._get_redis()
    index_str, _, _ = t1.partition(":")
    key = f"xianyu:browser:slot:{index_str}"

    # 模拟：t1 的 slot 已经因为 TTL 到期被另一个进程重新抢到
    await r.set(key, "someone-else-token")

    await limiter.release_browser_slot(t1)

    assert await r.get(key) == "someone-else-token"


@pytest.mark.anyio
async def test_release_logs_and_swallows_watch_error(limiter, monkeypatch):
    """release 遇到 WatchError（并发覆盖）时应该吞掉异常、不删除、不向上抛。

    fakeredis 不会真的触发 WatchError（见上一个测试的说明），这里用一层包装
    直接在 pipeline.execute() 上人为抛出 WatchError，只测试“遇到 WatchError
    该怎么处理”这段代码本身的逻辑，不测试 WATCH 的并发检测能力。
    """
    from redis.exceptions import WatchError

    class _RaisingPipeline:
        def __init__(self, real_pipe):
            self._real_pipe = real_pipe

        async def __aenter__(self):
            await self._real_pipe.__aenter__()
            return self

        async def __aexit__(self, *exc_info):
            return await self._real_pipe.__aexit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self._real_pipe, name)

        async def execute(self, *args, **kwargs):
            raise WatchError("模拟并发覆盖：fakeredis 无法真实触发，这里手工模拟")

    real_redis = limiter._get_redis()
    t1 = await limiter.acquire_browser_slot(timeout=1)
    index_str, _, _ = t1.partition(":")
    key = f"xianyu:browser:slot:{index_str}"

    original_pipeline = real_redis.pipeline

    def fake_pipeline(*args, **kwargs):
        return _RaisingPipeline(original_pipeline(*args, **kwargs))

    monkeypatch.setattr(real_redis, "pipeline", fake_pipeline)

    await limiter.release_browser_slot(t1)  # 不应向上抛异常

    monkeypatch.setattr(real_redis, "pipeline", original_pipeline)
    # WatchError 分支放弃删除，key 应该还在
    assert await real_redis.get(key) == t1.partition(":")[2]


@pytest.mark.anyio
async def test_renew_slot_extends_ttl_when_owner(limiter):
    """_renew_slot 在 value 仍是自己的情况下应该把 TTL 续回 _SLOT_TTL_SECONDS。"""
    t1 = await limiter.acquire_browser_slot(timeout=1)
    r = limiter._get_redis()
    index_str, _, _ = t1.partition(":")
    key = f"xianyu:browser:slot:{index_str}"

    await r.expire(key, 1)  # 模拟快到期
    ok = await limiter._renew_slot(t1)
    assert ok is True
    ttl = await r.ttl(key)
    assert ttl > 1  # 已经被续回接近 _SLOT_TTL_SECONDS


@pytest.mark.anyio
async def test_renew_slot_returns_false_when_not_owner(limiter):
    """_renew_slot 发现 value 不匹配时应该返回 False，且不动 TTL。"""
    r = limiter._get_redis()
    key = "xianyu:browser:slot:0"
    await r.set(key, "someone-else-token", ex=5)

    ok = await limiter._renew_slot("0:not-the-owner-token")

    assert ok is False
    ttl = await r.ttl(key)
    assert 0 < ttl <= 5  # 未被续期覆盖成新的完整 TTL


@pytest.mark.anyio
async def test_browser_slot_renews_ttl_while_held(limiter, monkeypatch):
    """browser_slot 持有期间应该定期续期，长任务不会被提前回收（审查 Finding 2）。

    把 TTL 和续期间隔都调小，故意持有超过原始 TTL 的时间：如果没有心跳续期，
    slot key 会在原始 TTL 后被 Redis 自动删除；有心跳续期的话，key 应该始终存在，
    直到退出 context 后才被释放。
    """
    monkeypatch.setattr(limiter, "_SLOT_TTL_SECONDS", 1)
    monkeypatch.setattr(limiter, "_RENEW_INTERVAL_SECONDS", 0.3)

    async with limiter.browser_slot(timeout=1) as token:
        r = limiter._get_redis()
        index_str, _, _ = token.partition(":")
        key = f"xianyu:browser:slot:{index_str}"

        await asyncio.sleep(1.5)  # 超过原始 TTL（1s），依赖心跳续期才能存活
        assert await r.exists(key) == 1

    assert await r.exists(key) == 0  # 退出 context 后应已释放


@pytest.mark.anyio
async def test_browser_slot_releases_even_if_renew_task_raises(limiter, monkeypatch):
    """心跳任务内部抛未捕获异常时，body 的正常结果不应被掩盖，槽位仍应正常释放
    （二轮复审 Finding A）。

    修复前：`finally` 里 `await renew_task` 没有 try/except，renew_task 一旦
    以异常结束就会把异常原样抛出，直接跳过下面的 release_browser_slot——一次
    成功跑完的浏览器任务会被心跳的内部异常掩盖成整体失败。
    """

    async def _boom(*_a, **_kw):
        raise RuntimeError("心跳内部炸了")

    monkeypatch.setattr(limiter, "_renew_slot", _boom)
    monkeypatch.setattr(limiter, "_RENEW_INTERVAL_SECONDS", 0.05)

    r = limiter._get_redis()
    result = None
    async with limiter.browser_slot(timeout=1) as token:
        index_str, _, _ = token.partition(":")
        key = f"xianyu:browser:slot:{index_str}"
        await asyncio.sleep(0.2)  # 给心跳任务机会跑一次并抛异常退出
        result = "body finished normally"

    assert result == "body finished normally"  # body 的正常结果没被心跳异常掩盖
    assert await r.exists(key) == 0  # 槽位仍被正确释放，没有因为心跳异常被跳过


@pytest.mark.anyio
async def test_browser_slot_releases_even_if_renew_task_cancelled(limiter, monkeypatch):
    """renew_task 被外部直接 cancel()（例如未来接入 TaskGroup/优雅关闭批量取消
    子任务）时，body 的正常结果不应被掩盖，槽位仍应正常释放（二轮复审 Finding A）。

    CancelledError 在 Python 3.8+ 继承自 BaseException，普通 `except Exception`
    抓不住，必须显式处理。
    """

    async def _hang(*_a, **_kw):
        await asyncio.sleep(10)
        return True

    monkeypatch.setattr(limiter, "_renew_slot", _hang)
    monkeypatch.setattr(limiter, "_RENEW_INTERVAL_SECONDS", 0.01)

    r = limiter._get_redis()
    result = None
    async with limiter.browser_slot(timeout=1) as token:
        index_str, _, _ = token.partition(":")
        key = f"xianyu:browser:slot:{index_str}"
        await asyncio.sleep(0.05)  # 让心跳任务先进入挂起的 _renew_slot 调用
        renew_task = next(
            t
            for t in asyncio.all_tasks()
            if not t.done() and t.get_coro().__name__ == "_renew_loop"
        )
        renew_task.cancel()  # 模拟外部批量取消
        result = "body finished normally"

    assert result == "body finished normally"
    assert await r.exists(key) == 0


@pytest.fixture()
def anyio_backend():
    return "asyncio"
