"""
滑块验证编排：远程求解 + 真人鼠标/Playwright 主引擎 + DrissionPage 兜底

对调用方暴露两组函数，职责按"是否需要本地浏览器"划分：

1. try_remote_captcha_solve / run_remote_captcha_solve：可选的远程过滑块，
   纯 HTTP 请求，不启动任何本地浏览器。调用方应在获取全局浏览器并发槽位
   （run_browser_task / WeightedTaskRunner）之前先调用，返回非 None 即为
   确定性结果，直接采用；返回 None 才继续下一步。
2. run_slider_verification_with_fallback：真人鼠标 → Playwright 主引擎 →
   DrissionPage 兜底，三者都会启动本地浏览器，调用方必须整体派发进
   run_browser_task/WeightedTaskRunner（全局浏览器并发槽位保护范围）。

两组函数拆开是为了避免远程求解（读超时下限 300 秒，链接过期时最多再重试
2 次，最坏约 900 秒）占用只有本地引擎才需要的浏览器并发槽位，把同一时段
其它真正需要浏览器的发布/续期任务饿死。
"""
from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional, Tuple

from loguru import logger

from common.services.captcha.slider_stealth import run_slider_verification, CAPTCHA_NOT_REQUIRED, URL_EXPIRED
from common.services.captcha.remote_timeout import get_remote_solve_timeout
from common.services.captcha.slider_mode import is_real_mouse_slider_mode
from common.services.captcha.drissionpage_slider import (
    run_drissionpage_verification,
    DRISSIONPAGE_AVAILABLE,
)


def _has_x5sec(cookies: Optional[Dict[str, str]]) -> bool:
    """判断 cookie 字典中是否包含 x5/x5sec 相关 cookie（与上层放行判定一致）。"""
    if not cookies:
        return False
    for name in cookies:
        name_lower = str(name).lower()
        if name_lower.startswith("x5") or "x5sec" in name_lower:
            return True
    return False


def _load_fallback_config() -> Tuple[bool, bool, int]:
    """读取兜底配置 (是否启用, 是否无头, 超时秒)。

    兼容从 websocket 的 app.core.config 或 common.core.config 读取。
    """
    enabled, headless, timeout = True, True, 25
    settings = None
    try:
        from app.core.config import get_settings
        settings = get_settings()
    except Exception:
        try:
            from common.core.config import get_settings
            settings = get_settings()
        except Exception:
            settings = None
    if settings is not None:
        enabled = bool(getattr(settings, "captcha_drissionpage_fallback_enabled", True))
        headless = bool(getattr(settings, "captcha_drissionpage_headless", True))
        timeout = int(getattr(settings, "captcha_drissionpage_timeout", 25))
    return enabled, headless, timeout


def _real_mouse_enabled() -> bool:
    """读取系统设置中的真实鼠标滑动方式。"""
    return is_real_mouse_slider_mode()


def is_real_mouse_enabled() -> bool:
    """返回真实鼠标开关，供线程池前置权重入口复用。"""
    return _real_mouse_enabled()


def _call_remote_solve(
    remote_url: str,
    remote_secret: str,
    user_id: str,
    url: str,
    browser_timeout: int,
    cookies_str: str = "",
    device_id: str = "",
) -> Tuple[str, Optional[Dict[str, str]], Optional[str]]:
    """调用远程过滑块接口。

    Args:
        cookies_str: 账号 Cookie（仅当"传递Cookie"开关开启时非空）。传入后远程端在
            遇到"抱歉，页面访问出现了问题"（链接过期）时，可凭此 Cookie 重取新链接继续处理。
        device_id: 设备 ID，配合 cookies_str 供远程端重新请求 token 接口使用。

    Returns:
        (status, cookies, message)
        status: 'ok'（远程通过，cookies 为 x5*）/ 'fail'（远程有返回但未通过）/
                'url_expired'（远程反馈验证链接已过期，调用方应刷新URL后重试）/
                'fallback'（超时或网络不可用，应回退本机逻辑）
        message: 远程接口返回的失败原因或本地解析原因
    """
    import requests

    payload = {
        "secret_key": remote_secret,
        "account_id": str(user_id),
        "url": url,
        "browser_timeout": int(browser_timeout),
    }
    # 仅在开启"传递Cookie"开关时携带账号 Cookie / 设备 ID（默认不传，保护账号隐私）
    if cookies_str:
        payload["cookies"] = cookies_str
        payload["device_id"] = device_id or ""

    try:
        resp = requests.post(
            remote_url,
            json=payload,
            # 连接 8s 内必须建立，读取给足远程求解时间；超时/连不上 → 回退本机
            timeout=(8, get_remote_solve_timeout(browser_timeout)),
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"【{user_id}】远程过滑块超时/不可用，回退本机逻辑: {e}")
        return "fallback", None, str(e)

    try:
        data = resp.json()
    except Exception as e:
        # 远程有响应但响应体异常：视为远程未通过（非超时 → 不回退）
        logger.warning(f"【{user_id}】远程过滑块响应解析失败，判失败（不回退）: {e}")
        return "fail", None, f"远程响应解析失败: {e}"

    if isinstance(data, dict) and data.get("success"):
        cookies = (data.get("data") or {}).get("cookies") or {}
        if cookies:
            return "ok", cookies, None
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message") or "").strip()
    # 远程明确反馈"验证链接已过期"：调用方需刷新URL后重试（老版本远程端无此字段，自然走 fail）
    if isinstance(data, dict) and (data.get("data") or {}).get("url_expired"):
        logger.info(f"【{user_id}】远程反馈验证链接已过期(url_expired)")
        return "url_expired", None, message or "远程反馈验证链接已过期"
    return "fail", None, message or "远程过滑块未通过"


def try_remote_captcha_solve(
    user_id: str,
    url: str,
    remote_config: dict,
    existing_cookies_str: str = "",
    url_provider: Optional[Callable[[], Optional[str]]] = None,
    browser_timeout: int = 20,
) -> Optional[Tuple[bool, Optional[Dict[str, str]], Optional[str]]]:
    """只做远程过滑块的 HTTP 请求，不涉及任何本地浏览器（同步阻塞函数）。

    审查 Finding B：本函数原来是 run_slider_verification_with_fallback 的
    第 -1 步，和后面『真人鼠标/Playwright主引擎/DrissionPage兜底』这些真正
    会启动本地浏览器的步骤挤在同一个函数体里，被调用方统一派发进
    run_browser_task/WeightedTaskRunner——也就是被包在全局浏览器并发槽位
    里。但远程求解全程只是一次 HTTP 请求，读超时下限 300 秒
    （common/services/captcha/remote_timeout.py 的
    _MIN_REMOTE_SOLVE_TIMEOUT_SECONDS），链接过期时最多再重试 2 次，最坏
    约 900 秒——这段时间里本机完全没有浏览器在跑，槽位却白白被占用一个，
    会把同一时段其它真正需要浏览器的发布/续期任务饿死到超时。
    因此把这一步从 run_slider_verification_with_fallback 中拆出来，调用方
    必须在【进入槽位保护的调度器之前】先调用本函数（经由下面的异步包装
    run_remote_captcha_solve，派发到不占用全局浏览器槽位的专用线程池）；
    只有本函数返回 None（未配置，或远程超时/不可用需要回退）时，才继续走
    run_slider_verification_with_fallback（那一步仍然、且必须走
    run_browser_task/WeightedTaskRunner，本函数不做任何本地浏览器兜底）。

    Returns:
        None：远程未配置（url/secret 为空），或远程超时/不可用
            （_call_remote_solve 返回 'fallback'）——调用方应该继续走
            run_slider_verification_with_fallback 的本机逻辑。
        (success, cookies, 'remote')：远程给出了确定性结果（成功，或明确
            失败/链接过期重试次数用尽），调用方应直接采用并返回，不需要
            也不应该再碰本地浏览器。
    """
    r_url = (remote_config.get("url") or "").strip()
    r_secret = (remote_config.get("secret") or "").strip()
    if not (r_url and r_secret):
        return None

    # 仅在开启"传递Cookie"开关时携带账号 Cookie / 设备 ID（默认不传）
    if remote_config.get("pass_cookies"):
        r_cookies = existing_cookies_str or ""
        r_device_id = (remote_config.get("device_id") or "")
    else:
        r_cookies = ""
        r_device_id = ""
    status, r_cookies_out, remote_message = _call_remote_solve(
        r_url, r_secret, user_id, url, browser_timeout, r_cookies, r_device_id
    )
    if status == "ok" and _has_x5sec(r_cookies_out):
        logger.info(f"【{user_id}】远程过滑块成功，采用远程结果")
        return True, r_cookies_out, "remote"
    # 远程反馈验证链接已过期：本端用 url_provider 重取新鲜链接后再调远程（最多 2 次），
    # 与本机处理链接过期保持一致；无 url_provider 或重试用尽则按失败处理（不回退）。
    remote_url_refreshes = 0
    max_remote_url_refreshes = 2 if url_provider is not None else 0
    while status == "url_expired" and remote_url_refreshes < max_remote_url_refreshes:
        remote_url_refreshes += 1
        logger.warning(
            f"【{user_id}】远程反馈验证链接已过期，第{remote_url_refreshes}次重取新链接后重试远程"
        )
        try:
            fresh = url_provider()
        except Exception as up_e:
            logger.warning(f"【{user_id}】重取验证链接异常: {up_e}")
            fresh = None
        if fresh == CAPTCHA_NOT_REQUIRED:
            logger.info(f"【{user_id}】重取链接时检测到 token 已可用，无需滑块，结束远程流程")
            return True, None, "remote"
        if not (fresh and isinstance(fresh, str)):
            logger.info(f"【{user_id}】重取验证链接失败，远程过滑块按失败处理（不回退）")
            return False, None, "remote"
        status, r_cookies_out, remote_message = _call_remote_solve(
            r_url, r_secret, user_id, fresh, browser_timeout, r_cookies, r_device_id
        )
        if status == "ok" and _has_x5sec(r_cookies_out):
            logger.info(f"【{user_id}】远程过滑块成功（刷新链接后），采用远程结果")
            return True, r_cookies_out, "remote"
    if status in ("fail", "url_expired"):
        reason = remote_message or "远程过滑块未通过"
        logger.info(f"【{user_id}】远程过滑块未通过（非超时），按配置不回退本机，返回失败: {reason}")
        return False, None, f"remote:{reason}"
    # status == 'fallback' → 让调用方回退到 run_slider_verification_with_fallback
    return None


_remote_solve_executor: Optional[ThreadPoolExecutor] = None
_remote_solve_executor_lock = threading.Lock()


def _get_remote_solve_executor() -> ThreadPoolExecutor:
    """远程过滑块 HTTP 请求专用线程池。

    故意不复用 common/services/captcha/concurrency.py 的
    get_browser_task_executor()：那个池子是按『全局浏览器并发槽位数 + 少量
    余量』定的容量，本来就偏小；远程求解是纯网络 IO、不需要浏览器互斥，
    混进同一个池子会在多个账号同时走远程时把这几百秒的 HTTP 请求占满该池
    的线程，反而挤掉真正需要浏览器的任务排队进度。也不用 asyncio 默认
    线程池（同样的理由：会和 aiohttp 的 DNS 解析共用池子）。
    """
    global _remote_solve_executor
    if _remote_solve_executor is None:
        with _remote_solve_executor_lock:
            if _remote_solve_executor is None:
                _remote_solve_executor = ThreadPoolExecutor(
                    max_workers=8,
                    thread_name_prefix="remote-captcha-solve",
                )
    return _remote_solve_executor


async def run_remote_captcha_solve(
    user_id: str,
    url: str,
    remote_config: dict,
    existing_cookies_str: str = "",
    url_provider: Optional[Callable[[], Optional[str]]] = None,
    browser_timeout: int = 20,
) -> Optional[Tuple[bool, Optional[Dict[str, str]], Optional[str]]]:
    """try_remote_captcha_solve 的异步包装：派发到专用线程池执行，
    不获取、也不经过全局浏览器并发槽位（见 try_remote_captcha_solve 的
    docstring）。调用方必须在获取槽位【之前】await 本函数；只有返回 None
    时才应该继续走槽位保护的 run_slider_verification_with_fallback。
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(
        try_remote_captcha_solve,
        user_id, url, remote_config, existing_cookies_str, url_provider, browser_timeout,
    )
    return await loop.run_in_executor(_get_remote_solve_executor(), call)


def run_slider_verification_with_fallback(
    user_id: str,
    url: str,
    enable_learning: bool = True,
    headless: bool = False,
    browser_timeout: int = 20,
    existing_cookies_str: str = "",
    url_provider: Optional[Callable[[], Optional[str]]] = None,
    weight_class: str = "local",
    slider_mode: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, str]], Optional[str]]:
    """真人鼠标/Playwright 主引擎 + DrissionPage 兜底的【本机】滑块验证编排。

    审查 Finding B 之后：远程过滑块（HTTP，不涉及本地浏览器）已经拆到
    try_remote_captcha_solve / run_remote_captcha_solve，调用方必须在进入
    槽位保护的调度器（run_browser_task / WeightedTaskRunner）之前先调用
    那两个函数；只有它们返回 None（未配置远程，或远程超时/不可用需要回退）
    时，才应该把本函数派发进 run_browser_task/WeightedTaskRunner 执行——
    本函数不再处理 remote_config，也不接受该参数，因此不存在"忘记回退到
    槽位保护的调度器、直接裸跑本地浏览器"的写法：本函数内部的三个引擎
    （真人鼠标/Playwright/DrissionPage）本来就都需要走同步阻塞的浏览器
    launch，只要调用方仍然用 run_browser_task/WeightedTaskRunner 派发本
    函数整体（而不是拆开单独调用内部某个引擎），槽位覆盖就必然完整，
    不需要额外校验。

    Args:
        user_id: 用户/账号 ID
        url: 验证页面 URL
        enable_learning: 主引擎是否启用轨迹学习
        headless: 主引擎是否无头
        browser_timeout: 主引擎单次超时（秒）
        existing_cookies_str: 现有 cookie 字符串，供兜底引擎注入
        url_provider: 可选回调，浏览器就绪后用于重新获取新鲜验证链接，规避等待槽位导致的链接过期
        weight_class: 排队来源类别（"local"=本地Token刷新 / "remote"=远程过滑块接口），
            仅 real_mouse 引擎排队时按权重放行使用；默认 "local"。
        slider_mode: 本次任务在入队前读取的滑动方式快照；未传时读取当前进程缓存。

    Returns:
        (是否成功, cookies 字典 | None, 通过引擎 | None)
        通过引擎取值：'playwright'（主引擎）/ 'drissionpage'（兜底引擎）/ 'real_mouse'（真实鼠标）/ None（未成功）
        （'remote' 不再从本函数返回，见 try_remote_captcha_solve）
    """
    # 0. 真实鼠标模式（在系统设置中选择）：
    #    用物理光标回放真人轨迹，成功率高但会占用桌面鼠标，仅限有桌面的 Windows。
    #    一旦开启且引擎可用：真实鼠标即为唯一引擎——成功返回成功；失败也【直接返回失败、不回退】
    #    原 CDP/DrissionPage 逻辑（避免低效且会被风控识破的 CDP 滑动；下次重试仍走真实鼠标）。
    #    仅当“开启了但引擎不可用”（非 Windows / 未装 pyautogui，属误配置）时，才回退原逻辑兜底。
    if is_real_mouse_slider_mode(slider_mode):
        real_mouse_available = False
        run_real_mouse_verification = None
        try:
            from common.services.captcha.real_mouse_slider import (
                run_real_mouse_verification as _rm_run,
                REAL_MOUSE_AVAILABLE as _rm_avail,
            )
            run_real_mouse_verification = _rm_run
            real_mouse_available = bool(_rm_avail)
        except Exception as imp_e:
            logger.warning(f"【{user_id}】真实鼠标引擎导入失败: {imp_e}")

        if real_mouse_available and run_real_mouse_verification is not None:
            logger.info(f"【{user_id}】启用真实鼠标滑块引擎（失败不回退，重试仍用真实鼠标）")
            try:
                rm_ok, rm_cookies = run_real_mouse_verification(
                    user_id, url,
                    existing_cookies_str=existing_cookies_str,
                    browser_timeout=max(browser_timeout, 40),
                    url_provider=url_provider,
                    weight_class=weight_class,
                )
            except Exception as rm_e:
                logger.warning(f"【{user_id}】真实鼠标引擎执行异常: {rm_e}")
                rm_ok, rm_cookies = False, None
            if rm_ok and _has_x5sec(rm_cookies):
                return True, rm_cookies, "real_mouse"
            # 验证链接已过期且无法自助重取：上报 url_expired，供远程调用方刷新URL后重试
            if rm_cookies == URL_EXPIRED:
                logger.info(f"【{user_id}】真实鼠标引擎检测到验证链接已过期，返回 url_expired")
                return False, None, "url_expired"
            # 按配置：真实鼠标失败不回退原引擎，直接返回失败
            logger.info(f"【{user_id}】真实鼠标未通过，按配置不回退，返回失败（下次重试仍用真实鼠标）")
            return False, None, None
        else:
            logger.error(
                f"【{user_id}】已选择真实鼠标滑动但引擎不可用"
                f"（需 Windows 桌面 + pyautogui），本次回退原有滑块逻辑"
            )

    # 1. Playwright 主引擎
    ok, cookies = run_slider_verification(
        user_id, url, enable_learning, headless, browser_timeout,
        url_provider=url_provider,
    )
    if ok and _has_x5sec(cookies):
        return True, cookies, "playwright"

    # 验证链接已过期且无法自助重取：上报 url_expired，供远程调用方刷新URL后重试
    # （过期页无需再走兜底引擎，兜底同样会命中过期页，直接返回让调用方刷新链接更高效）
    if cookies == URL_EXPIRED:
        logger.info(f"【{user_id}】主引擎检测到验证链接已过期，返回 url_expired")
        return False, None, "url_expired"

    # 2. 判断是否需要兜底
    fallback_enabled, fb_headless, fb_timeout = _load_fallback_config()
    if not fallback_enabled or not DRISSIONPAGE_AVAILABLE:
        if not fallback_enabled:
            logger.info(f"【{user_id}】DrissionPage 兜底未启用，返回主引擎结果")
        return ok, cookies, ("playwright" if (ok and cookies) else None)

    # 3. DrissionPage 兜底
    # 兜底前同样尝试刷新链接，避免主引擎耗时后链接再次过期
    fb_url = url
    if url_provider is not None:
        try:
            fresh = url_provider()
            if fresh == CAPTCHA_NOT_REQUIRED:
                # token 已可用、风控已解除，无需滑块，跳过兜底引擎（由上层采用新 token）
                logger.info(f"【{user_id}】检测到 token 已可用，跳过 DrissionPage 兜底引擎")
                return ok, cookies, ("playwright" if (ok and cookies) else None)
            if fresh and isinstance(fresh, str):
                fb_url = fresh
                logger.info(f"【{user_id}】兜底引擎使用刷新后的验证链接")
        except Exception as up_e:
            logger.warning(f"【{user_id}】兜底前刷新验证链接失败，沿用原链接: {up_e}")

    logger.info(f"【{user_id}】主引擎滑块未通过，启用 DrissionPage 兜底引擎重试")
    ok2, cookies2 = run_drissionpage_verification(
        user_id, fb_url, existing_cookies_str=existing_cookies_str,
        headless=fb_headless, browser_timeout=fb_timeout,
    )
    if ok2 and _has_x5sec(cookies2):
        return True, cookies2, "drissionpage"

    # 4. 兜底也未取得 x5sec：优先保留主引擎"成功但无 x5sec"的结果，
    #    以维持上层原有的"无 x5sec 不计入禁用"语义。
    if ok and cookies:
        return ok, cookies, "playwright"
    return ok2, cookies2, None
