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


@pytest.fixture()
def anyio_backend():
    return "asyncio"
