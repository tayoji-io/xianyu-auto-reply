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
- websocket/app/api/routes/password_login.py 的 _run_password_login_sync
  （原来裸 threading.Thread 跑同步 Playwright、完全绕开限流的缺口）
- common/services/captcha/orchestrator.py 的 try_remote_captcha_solve /
  run_remote_captcha_solve（审查 Finding B：远程过滑块是纯 HTTP 请求，
  不该占用全局浏览器并发槽位）

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


@pytest.fixture()
def websocket_password_login_module():
    """按需把 sys.path 切到 websocket/ 并加载 app.api.routes.password_login，
    用完恢复原状。

    websocket 和 backend-web 的顶层包都叫 `app`，同一个 pytest 进程里不能
    同时缓存两份——本文件其它用例已经在导入 backend-web 的 `app.services.
    xianyu_publisher` 等模块，所以这里必须先清掉 sys.modules 里所有 `app`
    前缀的缓存、把 websocket 目录排到 sys.path 最前面，用完后再原样恢复，
    避免污染同一 session 里跑在它前面或后面的其它用例。
    """
    ws_root = str(ROOT / "websocket")
    bw_root = str(ROOT / "backend-web")

    saved_path = list(sys.path)
    saved_app_modules = {
        name: mod for name, mod in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    for name in saved_app_modules:
        del sys.modules[name]

    sys.path[:] = [p for p in sys.path if p != bw_root]
    if ws_root not in sys.path:
        sys.path.insert(0, ws_root)

    try:
        import app.api.routes.password_login as m
        yield m
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = saved_path
        sys.modules.update(saved_app_modules)


def test_password_login_sync_finishes_session_on_slot_timeout(
    websocket_password_login_module, monkeypatch
):
    """Finding A 回归（审查发现）：run_browser_task 可能在
    _run_password_login_core 还没开始执行前就抛出异常（典型如等待全局浏览器
    并发槽位超时 BrowserSlotTimeout——人脸验证等待、批量发布复用浏览器等
    场景会让槽位被占用远超默认 120s 超时）。这种异常发生时
    _run_password_login_core 自身的 try/except/finally 完全没有机会执行，
    _run_password_login_sync 必须自己兜底：把 session 状态置为 failed、
    并调用 password_login_state.finish_processing——否则前端会一直轮询到
    1 小时会话清理，且该账号 5 分钟内无法重新发起登录。

    本用例是纯同步测试（不用 pytest.mark.anyio）：_run_password_login_sync
    内部会调用 asyncio.run()，不能在一个已经有运行中事件循环的协程里调用，
    因此像生产环境一样把它放到一个独立线程里跑。
    """
    m = websocket_password_login_module

    account_id = "test-acct-slot-timeout"
    session_id = "test-session-slot-timeout"
    m.password_login_sessions[session_id] = {"status": "processing", "error": None}

    import app.services.captcha.concurrency as conc_mod
    from app.services.captcha.password_login_state import password_login_state
    from common.services.browser_limiter import BrowserSlotTimeout

    password_login_state.start_processing(account_id)
    assert password_login_state.is_processing(account_id)

    async def _boom(*args, **kwargs):
        raise BrowserSlotTimeout("测试用：模拟等待全局浏览器并发槽位超时")

    monkeypatch.setattr(conc_mod, "run_browser_task", _boom)
    # should_skip_account 对未知账号走内存快速路径直接返回 False（见
    # common/services/captcha/concurrency.py），测试账号不在禁用列表里，
    # 不需要额外 mock。

    result: dict = {}

    def _invoke():
        try:
            m._run_password_login_sync(
                session_id, account_id, "test-account", "test-password", False, 1,
            )
        except Exception as e:  # 断言里再判定；这里只是防止线程吞掉异常导致误判超时
            result["thread_exception"] = e

    thread = threading.Thread(target=_invoke)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), "_run_password_login_sync 应该已经返回，而不是挂住"
    assert "thread_exception" not in result, (
        f"_run_password_login_sync 本身不应该再抛出异常: {result.get('thread_exception')}"
    )

    session = m.password_login_sessions[session_id]
    assert session["status"] == "failed", f"槽位超时后 session 状态应置为 failed，实际: {session}"
    assert "登录任务执行异常" in (session.get("error") or "")
    assert not password_login_state.is_processing(account_id), (
        "槽位超时后必须调用 finish_processing，否则该账号 5 分钟内无法重新发起登录"
    )


def test_try_remote_captcha_solve_returns_none_when_not_configured():
    """remote_config 为空 / 缺 url 或 secret 时，应返回 None（调用方据此走
    本机 run_slider_verification_with_fallback），而不是抛异常或误判成功。
    """
    from common.services.captcha import orchestrator as orch_mod

    assert orch_mod.try_remote_captcha_solve("u", "http://x.invalid/page", {}) is None
    assert orch_mod.try_remote_captcha_solve(
        "u", "http://x.invalid/page", {"url": "", "secret": ""}
    ) is None


def test_run_slider_verification_with_fallback_no_longer_takes_remote_config():
    """审查 Finding B：远程过滑块已经拆到 try_remote_captcha_solve /
    run_remote_captcha_solve，run_slider_verification_with_fallback 不应该
    再接受 remote_config 参数——这样调用方就不可能一不小心把远程逻辑传回
    这个会被整体塞进全局浏览器并发槽位的函数里。
    """
    import inspect
    from common.services.captcha import orchestrator as orch_mod

    sig = inspect.signature(orch_mod.run_slider_verification_with_fallback)
    assert "remote_config" not in sig.parameters


@pytest.mark.anyio
async def test_run_remote_captcha_solve_does_not_hold_global_slot(fake_limiter, monkeypatch):
    """Finding B 回归：远程过滑块（common/services/captcha/orchestrator.py 的
    run_remote_captcha_solve）全程只是一次 HTTP 请求，不涉及任何本地浏览器，
    因此不应该占用全局浏览器并发槽位——否则读超时下限 300 秒、链接过期时
    最多再重试 2 次、最坏约 900 秒的等待，会把该时段其它真正需要浏览器的
    发布/续期任务饿死。

    验证方式：MAX_BROWSER_CONCURRENT=1 时先用 browser_slot() 占住唯一的
    全局槽位，再在槽位仍被占用的情况下调用 run_remote_captcha_solve——如果
    它误用了全局槽位，会因为抢不到槽位而卡住，下面的 asyncio.wait_for 会
    超时失败；如果它确实不碰全局槽位，应该能立刻跑完并返回远程结果。
    """
    from common.services.captcha import orchestrator as orch_mod
    from common.services.browser_limiter import browser_slot

    def _fake_call_remote_solve(
        remote_url, remote_secret, user_id, url, browser_timeout,
        cookies_str="", device_id="",
    ):
        # 模拟远程 HTTP 请求本身的耗时（真实场景里是网络 IO，这里用 sleep
        # 代替，验证的是"不需要等全局槽位"，不是远程请求本身的实现）。
        time.sleep(0.2)
        return "ok", {"x5sec": "abc"}, None

    monkeypatch.setattr(orch_mod, "_call_remote_solve", _fake_call_remote_solve)

    async with browser_slot():  # 占住唯一的全局槽位，且持有到本 with 块结束
        result = await asyncio.wait_for(
            orch_mod.run_remote_captcha_solve(
                "user1", "http://x.invalid/page",
                {"url": "http://remote.invalid/solve", "secret": "s"},
            ),
            timeout=2,
        )

    assert result == (True, {"x5sec": "abc"}, "remote"), (
        "run_remote_captcha_solve 应该能在全局槽位被占满时依然正常跑完，"
        "证明它没有去竞争这个槽位"
    )
