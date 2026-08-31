# tests/test_browser_slot_wiring.py
"""验证浏览器并发限流槽位（Task 4 产出，见 tests/test_browser_limiter.py）已经
正确接入到本任务（Task 5）负责的各个调度/编排层：

- common/services/captcha/concurrency.py 的 run_browser_task
  （cookie_renew_browser_service / cookies_refresh_service / 密码登录 /
  滑块验证主引擎+DrissionPage兜底 的公共同步任务调度入口）
- common/services/captcha/weighted_runner.py 的 WeightedTaskRunner
  （真实鼠标模式下绕开 run_browser_task 的第二条调度路径）
- backend-web/app/services/xianyu_publisher.py 的 XianyuPublisher
  （initialize()/close() 分离、且发布批次会跨多次调用复用同一浏览器）
- backend-web/app/services/search/browser.py 的 BrowserManager
  （init_browser()/close_browser() 同构的分离生命周期）

本文件只验证“调度/编排层是否正确获取并释放了全局槽位”，不重复验证限流器
自身的正确性（SET NX、WATCH/MULTI/EXEC、心跳续期等）——那部分已经在
tests/test_browser_limiter.py 里覆盖，本任务不修改该文件。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend-web"))


@pytest.fixture()
def fake_limiter(monkeypatch):
    """把全局浏览器并发槽位指向 fakeredis，且默认上限设为 1，
    方便测试用“两个任务的执行区间是否重叠”直接观察串行化效果。
    """
    import fakeredis.aioredis
    from common.services import browser_limiter as mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(mod, "_get_redis", lambda: fake)
    monkeypatch.setenv("MAX_BROWSER_CONCURRENT", "1")
    return mod


def _record_window(windows: list, lock: threading.Lock, name: str, hold_seconds: float = 0.2):
    """模拟一次“同步、阻塞”的浏览器任务，记录其真实执行的起止时间。"""
    start = time.monotonic()
    time.sleep(hold_seconds)
    end = time.monotonic()
    with lock:
        windows.append((name, start, end))


def _assert_non_overlapping(windows: list) -> None:
    assert len(windows) == 2, windows
    (_, s1, e1), (_, s2, e2) = sorted(windows, key=lambda w: w[1])
    assert e1 <= s2, f"两次浏览器任务的执行区间发生了重叠，说明全局槽位没有生效: {windows}"


@pytest.mark.anyio
async def test_run_browser_task_serializes_via_global_slot(fake_limiter):
    """run_browser_task 是 cookie_renew_browser_service / cookies_refresh_service /
    密码登录（经 password_login.py 的 asyncio.run 桥接）/ 滑块验证主引擎与
    DrissionPage 兜底 的公共调度入口。MAX_BROWSER_CONCURRENT=1 时两次并发调用
    的执行区间不能重叠——若重叠，说明 run_browser_task 没有真正持有全局槽位，
    只是被线程池本身的并发度巧合限制住（线程池本身允许 >1 并发，见下方
    max_workers 断言）。
    """
    from common.services.captcha import concurrency as conc_mod

    assert conc_mod.get_browser_task_executor()._max_workers >= 2, (
        "本测试要求线程池本身允许 >=2 并发，否则无法证明串行化来自全局槽位"
        "而非线程池容量"
    )

    windows: list = []
    lock = threading.Lock()

    await asyncio.gather(
        conc_mod.run_browser_task(_record_window, windows, lock, "a"),
        conc_mod.run_browser_task(_record_window, windows, lock, "b"),
    )

    _assert_non_overlapping(windows)


@pytest.mark.anyio
async def test_weighted_runner_dispatch_loop_uses_global_slot(fake_limiter):
    """real_mouse 前置加权队列（SLIDER_MODE_REAL_MOUSE 时被调用方选中的调度
    路径）绕开 run_browser_task、直接对公共浏览器执行器提交任务，因此需要
    单独在 _dispatch_loop 里再包一次全局槽位。这里用一个 4 worker 的独立
    执行器（而不是默认的 1 worker 测试执行器）验证串行化确实来自全局槽位，
    不是巧合于执行器本身只有 1 个线程。
    """
    from common.services.captcha.weighted_runner import WeightedTaskRunner

    test_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wr-test")
    runner = WeightedTaskRunner(
        lambda: {"local": 1.0, "remote": 1.0},
        executor_factory=lambda: test_executor,
    )

    windows: list = []
    lock = threading.Lock()

    try:
        await asyncio.gather(
            runner.submit("local", _record_window, windows, lock, "a"),
            runner.submit("remote", _record_window, windows, lock, "b"),
        )
    finally:
        runner.shutdown()
        test_executor.shutdown(wait=True, cancel_futures=True)

    _assert_non_overlapping(windows)


def _make_fake_async_playwright():
    """构造一个满足 XianyuPublisher.initialize() / BrowserManager.init_browser()
    调用链的最小 Playwright async_api 假对象：
    async_playwright() -> .start() -> playwright
        .chromium.launch(**kw) / .launch_persistent_context(dir, **kw) -> browser/context
    browser.new_context(**kw) -> context；context.new_page() -> page
    """
    page = MagicMock()
    page.add_init_script = AsyncMock()
    page.close = AsyncMock()
    page.set_default_timeout = MagicMock()
    page.set_default_navigation_timeout = MagicMock()

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    context.pages = []
    context.browser = MagicMock()
    context.on = MagicMock()

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    playwright_obj = MagicMock()
    playwright_obj.chromium.launch = AsyncMock(return_value=browser)
    playwright_obj.chromium.launch_persistent_context = AsyncMock(return_value=context)
    playwright_obj.stop = AsyncMock()

    async_playwright_factory = MagicMock()
    async_playwright_factory.return_value.start = AsyncMock(return_value=playwright_obj)
    return async_playwright_factory


@pytest.mark.anyio
async def test_xianyu_publisher_holds_slot_across_initialize_and_close(fake_limiter, monkeypatch):
    """XianyuPublisher.initialize()/close() 是两个独立方法，发布批次会以
    reuse_browser=True、should_close=False 跨多次 publish_item() 调用复用
    同一浏览器（backend-web/app/services/publish_execution_service.py）。
    验证：
    1) initialize() 之后槽位已被占用（第二个发布器在 MAX_BROWSER_CONCURRENT=1
       下无法立刻拿到槽位，必须等第一个 close() 之后才能拿到）——证明槽位真的
       覆盖了“浏览器还活着”的整个区间，而不是 initialize() 一返回就释放；
    2) 重复 initialize()（模拟 reuse_browser 复用路径）不会重复占用槽位；
    3) close() 之后槽位被释放，另一个等待者可以立刻拿到。
    """
    import app.services.xianyu_publisher as pub_mod
    from common.services.browser_limiter import browser_slot as real_browser_slot

    fake_factory = _make_fake_async_playwright()
    monkeypatch.setattr(pub_mod, "async_playwright", fake_factory)
    monkeypatch.setattr(pub_mod, "ensure_playwright_browser_path", lambda: None)
    monkeypatch.setattr(pub_mod, "get_chromium_executable_path", lambda: None)
    # 生产代码里 browser_slot() 用默认 120s 超时；测试改用短超时，
    # 只是让“拿不到槽位”这件事更快暴露出来，不改变生产行为。
    monkeypatch.setattr(pub_mod, "browser_slot", lambda: real_browser_slot(timeout=0.3))

    publisher = pub_mod.XianyuPublisher()
    assert publisher._browser_slot_cm is None

    await publisher.initialize(headless=True)
    assert publisher._browser_slot_cm is not None, "initialize() 之后应已持有全局槽位"

    # 模拟“批量发布复用浏览器”：is_initialized=True 且不强制重建时应直接复用，
    # 不重新走 launch、也不重复占用槽位。
    held_cm = publisher._browser_slot_cm
    await publisher.initialize(headless=True)  # is_initialized=True, force_reinit=False
    assert publisher._browser_slot_cm is held_cm, "复用路径不应重新获取新的槽位"

    # 第二个发布器此时应该拿不到槽位（MAX_BROWSER_CONCURRENT=1 且第一个仍持有）。
    from common.services.browser_limiter import BrowserSlotTimeout

    other = pub_mod.XianyuPublisher()
    monkeypatch.setattr(pub_mod, "async_playwright", _make_fake_async_playwright())
    with pytest.raises(BrowserSlotTimeout):
        await other.initialize(headless=True)

    # 第一个 close() 之后释放槽位，第二个应能立刻拿到。
    await publisher.close()
    assert publisher._browser_slot_cm is None

    await other.initialize(headless=True)
    assert other._browser_slot_cm is not None
    await other.close()
    assert other._browser_slot_cm is None


@pytest.mark.anyio
async def test_browser_manager_holds_slot_across_init_and_close(fake_limiter, monkeypatch):
    """search/browser.py 的 BrowserManager：init_browser()/close_browser() 与
    XianyuPublisher 同构，验证槽位同样覆盖了完整生命周期。
    """
    import app.services.search.browser as browser_mod
    from common.services.browser_limiter import browser_slot as real_browser_slot

    fake_factory = _make_fake_async_playwright()
    monkeypatch.setattr(browser_mod, "async_playwright", fake_factory)
    monkeypatch.setattr(browser_mod, "ensure_playwright_browser_path", lambda: None)
    monkeypatch.setattr(browser_mod, "get_chromium_executable_path", lambda: None)
    monkeypatch.setattr(browser_mod, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(browser_mod, "browser_slot", lambda: real_browser_slot(timeout=0.3))

    manager = browser_mod.BrowserManager()
    assert manager._browser_slot_cm is None

    await manager.init_browser(headless=True)
    assert manager._browser_slot_cm is not None

    from common.services.browser_limiter import BrowserSlotTimeout

    other = browser_mod.BrowserManager()
    monkeypatch.setattr(browser_mod, "async_playwright", _make_fake_async_playwright())
    with pytest.raises(BrowserSlotTimeout):
        await other.init_browser(headless=True)

    await manager.close_browser()
    assert manager._browser_slot_cm is None

    await other.init_browser(headless=True)
    assert other._browser_slot_cm is not None
    await other.close_browser()
    assert other._browser_slot_cm is None
