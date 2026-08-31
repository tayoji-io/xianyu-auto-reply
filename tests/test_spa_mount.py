"""SPA 挂载必须位于 API 路由之后，不能吞掉 /api/v1 与 /health。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend-web"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>SPA-ROOT</html>", encoding="utf-8")
    monkeypatch.setenv("WEB_DIR", str(web))
    # `_bootstrap` 在模块导入时就创建 app 并根据当次 WEB_DIR 执行挂载逻辑；
    # 一旦进入 sys.modules，后续测试单纯 `import _bootstrap` 只会命中缓存，
    # 拿到上一个测试遗留的旧 app（挂载的是上一个 tmp_path 目录）。
    # 因此每个测试前都强制丢弃缓存，确保重新执行模块顶层代码、按当次环境变量重建 app。
    sys.modules.pop("_bootstrap", None)
    from _bootstrap import app  # noqa: WPS433 延迟导入，确保环境变量已设置
    return TestClient(app)


def test_root_serves_spa(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "SPA-ROOT" in r.text


def test_health_not_swallowed_by_spa(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "SPA-ROOT" not in r.text


def test_unknown_frontend_route_falls_back_to_spa(client):
    r = client.get("/accounts")
    assert r.status_code == 200
    assert "SPA-ROOT" in r.text


def test_unregistered_api_route_not_swallowed_by_spa(client):
    """`/api/v1/*` 走的是 `_IncludedRouter` 惰性展开路径（与 `/health` 不同）。

    审查发现：早期实现里，请求一条**根本不存在**的 API 路径
    （拼错的/已下线的/尚未实现的接口）会被 `_IncludedRouter` 判定为
    Match.NONE，进而透传给排在其后的 "/" SPA 挂载，被伪装成 200 的
    SPA 首页 HTML —— 这比"吞掉一条已注册路径"更隐蔽也更危险：调用方
    按状态码判断成败时，会把一个不存在的接口误判为"请求成功"。

    这里刻意选一条在 /openapi.json 中确认**不存在**的路径，断言它
    仍然是 404，而不是被 SPA 吞成 200 的 HTML。
    """
    openapi = client.get("/openapi.json").json()
    target = "/api/v1/definitely-not-a-real-endpoint"
    assert target not in openapi["paths"], (
        f"{target} 意外出现在 openapi 路由表中，请换一条确认不存在的路径"
    )

    r = client.get(target)
    assert not (r.status_code == 200 and "SPA-ROOT" in r.text), (
        f"{target} 被 SPA 挂载吞掉了：返回了 200 的 SPA HTML，而不是 404"
    )
    assert r.status_code == 404


def test_health_trailing_slash_not_swallowed_by_spa(client):
    """`/health/`（比真实路由多一个尾部斜杠）不能被 SPA 挂载吞掉。

    审查发现：`/health` 本身是普通 APIRoute，能被正确保护；但它的畸形
    变体 `/health/` 并不会匹配到这条路由，同样会透传给 "/" SPA 挂载，
    被伪装成 200 的 SPA 首页 HTML。
    """
    r = client.get("/health/")
    assert not (r.status_code == 200 and "SPA-ROOT" in r.text), (
        "/health/ 被 SPA 挂载吞掉了：返回了 200 的 SPA HTML，而不是 404"
    )
    assert r.status_code == 404


def test_method_mismatch_on_real_endpoint_stays_405(client):
    """真实接口方法不对时必须是 405，不能降级成 404，更不能是 SPA 200。

    审查 Finding A：`/api/v1/auth/login` 只接受 POST。回合 2 的实现里，
    这类"路径已注册、方法不对"的请求会先被 SPA Mount 吞成 200 SPA HTML，
    修好吞掉问题之后又一度被简化成了 404——405 和 404 对调用方是两个不同
    信号（"方法用错了" vs "接口不存在"），网关/监控/按状态码分支的客户端
    会因此误判，必须保持 405。
    """
    r = client.get("/api/v1/auth/login")
    assert not (r.status_code == 200 and "SPA-ROOT" in r.text), (
        "/api/v1/auth/login (GET) 被 SPA 挂载吞掉了：返回了 200 的 SPA HTML"
    )
    assert r.status_code == 405


def test_double_slash_variant_not_swallowed_by_spa(client):
    """`//api/v1/x`（双斜杠变体）不能穿透保留前缀护栏被 SPA 吞掉。

    审查 Finding B：护栏原先用裸 `path.startswith("/api")` 判断，
    `//api/v1/x` 不满足这个判断（多了一个前导斜杠），会绕过护栏。

    注意：必须把完整绝对 URL（含 scheme+host）交给 TestClient，而不是只传
    `"//api/v1/x"`——httpx 会把以 `//` 开头的裸字符串当成"network-path
    reference"（RFC 3986 5.3）去和 base_url 做引用解析，从而丢失/替换掉
    路径中的双斜杠，测不到真正发到服务端的请求路径。
    """
    r = client.get("http://testserver//api/v1/x")
    assert not (r.status_code == 200 and "SPA-ROOT" in r.text), (
        "//api/v1/x 被 SPA 挂载吞掉了：返回了 200 的 SPA HTML，而不是 404"
    )
    assert r.status_code == 404


def test_reserved_prefix_lookalike_still_falls_back_to_spa(client):
    """`/apiary`、`/healthy-tips` 这类"长得像保留前缀但其实是合法前端路由
    名字"的路径，必须依然正常回退到 SPA——不能被裸 `startswith` 误堵。

    审查 Finding C：护栏原先用裸 `str.startswith()` 判断保留前缀，
    没有做路径段边界区分，会把这类路径也误判为落在 /api、/health 保留
    前缀下。这条测试的断言方向和其余几条相反：这里要求"确实回退到 SPA"。
    """
    for path in ("/apiary", "/healthy-tips"):
        r = client.get(path)
        assert r.status_code == 200 and "SPA-ROOT" in r.text, (
            f"{path} 被保留前缀护栏误堵了，应该正常回退到 SPA"
        )


def test_docs_redoc_variants_not_swallowed_by_spa(client):
    """`/docs`、`/redoc`、`/openapi.json` 的裸路径本身不受影响（各自是独立
    注册的 Route），但它们的子路径变体必须同样不能被 SPA 吞掉。

    审查 Finding D：`/docs/whatever`、`/docs/`、`/redoc/` 这些变体在
    "裸路径正常工作"的假象下被漏保护，同样会透传给 SPA 挂载。
    """
    # 裸路径不受影响，仍然是文档/schema 本身的真实内容。
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 200
        assert "SPA-ROOT" not in r.text

    # 子路径变体必须被护栏拦住，不能被 SPA 吞成 200 HTML。
    for path in ("/docs/whatever", "/docs/", "/redoc/"):
        r = client.get(path)
        assert not (r.status_code == 200 and "SPA-ROOT" in r.text), (
            f"{path} 被 SPA 挂载吞掉了：返回了 200 的 SPA HTML，而不是 404"
        )
        assert r.status_code == 404
