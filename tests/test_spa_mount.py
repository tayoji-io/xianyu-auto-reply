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


def test_real_api_route_not_swallowed_by_spa(client):
    """`/api/v1/*` 走的是 `_IncludedRouter` 惰性展开路径（与 `/health` 不同），

    必须单独验证：SPA 的 `/` 挂载不能把它吞掉、回退成 SPA 首页 HTML。
    用 /openapi.json 强制展开路由表，挑一条真实存在的 API 路径请求。
    判据：只要响应不是 200+SPA HTML，就说明请求命中了 API 路由（未被 SPA 吞掉）。
    401/403/405/422 均视为通过——它们是 API 路由自身的鉴权/校验行为。
    """
    openapi = client.get("/openapi.json").json()
    real_paths = [p for p in openapi["paths"] if p.startswith("/api/v1/")]
    assert real_paths, "openapi.json 中应至少包含一条 /api/v1/* 路径"

    target = "/api/v1/users/me"
    assert target in real_paths, f"{target} 不在 openapi 路由表中，请改选一条真实存在的路径"

    r = client.get(target)
    assert not (r.status_code == 200 and "SPA-ROOT" in r.text), (
        f"{target} 被 SPA 挂载吞掉了：返回了 200 的 SPA HTML，而不是 API 响应"
    )
    assert r.status_code in (401, 403, 405, 422)
