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
