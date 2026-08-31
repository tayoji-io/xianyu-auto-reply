"""平台注入网关环境变量时，AI 默认配置应指向网关且默认启用。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend-web"))


def _reload(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    import app.services.ai_reply_service as mod
    return importlib.reload(mod)


def test_defaults_point_to_gateway(monkeypatch):
    mod = _reload(
        monkeypatch,
        OPENAI_BASE_URL="https://www.runjobs.ai/v1",
        OPENAI_API_KEY="rj_test123",
        AI_MODEL="deepseek-chat",
    )
    s = mod.DEFAULT_AI_SETTINGS
    assert s["base_url"] == "https://www.runjobs.ai/v1"
    assert s["api_key"] == "rj_test123"
    assert s["model_name"] == "deepseek-chat"
    assert s["ai_enabled"] is True


def test_falls_back_when_gateway_absent(monkeypatch):
    mod = _reload(monkeypatch, OPENAI_BASE_URL=None, OPENAI_API_KEY=None, AI_MODEL=None)
    s = mod.DEFAULT_AI_SETTINGS
    assert s["api_key"] == ""
    assert s["ai_enabled"] is False
    assert s["model_name"] == "qwen-plus"


def test_stored_empty_api_key_falls_back_to_gateway_default(monkeypatch):
    """账号 metadata 中历史遗留的空字符串 api_key（如旧系统 Excel 导入、或早于本次
    迁移就保存过的空表单）不应把新的网关 key 覆盖成空。

    背景：`_extract_settings` 原先用 `if v is not None` 判断哪些 stored 字段生效，
    空字符串不是 None，会被当作“账号显式设置”盖掉默认值。这里验证修复后，
    stored 中的空字符串 api_key 会被当作“未配置”，从而回落到网关默认值。
    """
    mod = _reload(
        monkeypatch,
        OPENAI_BASE_URL="https://www.runjobs.ai/v1",
        OPENAI_API_KEY="rj_test123",
        AI_MODEL="deepseek-chat",
    )

    class _FakeAccount:
        metadata_json = {"ai_reply_settings": {"api_key": ""}}

    service = mod.AIReplySettingsService.__new__(mod.AIReplySettingsService)
    settings = service._extract_settings(_FakeAccount())
    assert settings["api_key"] == "rj_test123"


def test_stored_empty_model_name_falls_back_to_gateway_default(monkeypatch):
    """同上，针对 model_name：历史/导入数据里常见 model_name 存成 ""，
    应回落到网关默认模型（AI_MODEL 环境变量），而不是保持空白。
    """
    mod = _reload(
        monkeypatch,
        OPENAI_BASE_URL="https://www.runjobs.ai/v1",
        OPENAI_API_KEY="rj_test123",
        AI_MODEL="deepseek-chat",
    )

    class _FakeAccount:
        metadata_json = {"ai_reply_settings": {"model_name": ""}}

    service = mod.AIReplySettingsService.__new__(mod.AIReplySettingsService)
    settings = service._extract_settings(_FakeAccount())
    assert settings["model_name"] == "deepseek-chat"


def test_stored_blank_custom_prompts_is_not_overwritten_by_ai_persona(monkeypatch):
    """回归测试（对应审查 Finding 1）：custom_prompts 存 "" 的真实含义是
    “用户主动清空/不使用自定义人设”，是一次有意义的保存结果，不是“未配置”。

    即使平台注入了非空的 AI_PERSONA 默认人设，账号 stored 里显式留空的
    custom_prompts 读回来也必须仍是空字符串，不能被默认人设静默覆盖——
    否则会用平台默认人设改变 AI 代表卖家跟买家对话的语气和内容。
    """
    mod = _reload(
        monkeypatch,
        OPENAI_BASE_URL="https://www.runjobs.ai/v1",
        OPENAI_API_KEY="rj_test123",
        AI_MODEL="deepseek-chat",
        AI_PERSONA="你是一个热情的客服，喜欢用表情包",
    )

    class _FakeAccount:
        metadata_json = {"ai_reply_settings": {"custom_prompts": ""}}

    service = mod.AIReplySettingsService.__new__(mod.AIReplySettingsService)
    settings = service._extract_settings(_FakeAccount())
    assert settings["custom_prompts"] == ""
