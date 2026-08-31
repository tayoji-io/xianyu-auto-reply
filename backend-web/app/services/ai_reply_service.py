"""
AI回复设置服务

功能：
1. 管理账号的AI回复配置
2. 存储在账号metadata JSON中
3. 支持模型名称、API密钥、折扣设置等
"""
from __future__ import annotations

import os

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.services.ai_provider_service import (
    DEFAULT_AI_BASE_URL,
    DEFAULT_AI_PROVIDER_TYPE,
    clean_ai_text,
    get_ai_settings_missing_fields,
    normalize_ai_provider_type,
    read_ai_enabled,
)
from common.models.xy_account import XYAccount

# 平台（RunJobs）注入的模型网关；未注入时退回原有 dashscope 默认值
_GATEWAY_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
_GATEWAY_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

DEFAULT_AI_SETTINGS = {
    # 注意：这个值只对直接读取 DEFAULT_AI_SETTINGS 字典的调用方（如单测）可见。
    # 在真实账号读取路径 _extract_settings 里，本字段会被下方
    # `payload["ai_enabled"] = read_ai_enabled(stored)` 无条件覆盖——
    # 对从未存过 ai_reply_settings 的全新账号，read_ai_enabled({}) 恒为 False。
    # 也就是说"有网关 key 时默认开启"目前只体现在 api_key/base_url/model_name
    # 这几个字段上，AI 自动回复本身默认仍是关闭的，需要用户主动打开开关
    # ——这是有意为之的产品决策（自动回复会消耗用户的 RunJobs 余额、并代表
    # 用户与买家对话，默认开启等于替用户做重大决定），不是待修的 bug。
    "ai_enabled": bool(_GATEWAY_API_KEY),
    "provider_type": DEFAULT_AI_PROVIDER_TYPE,
    "model_name": os.getenv("AI_MODEL", "").strip() or "qwen-plus",
    "api_key": _GATEWAY_API_KEY,
    "base_url": _GATEWAY_BASE_URL or DEFAULT_AI_BASE_URL,
    "max_discount_percent": 10,
    "max_discount_amount": 100,
    "max_bargain_rounds": 3,
    "custom_prompts": os.getenv("AI_PERSONA", ""),
    "ai_time_range_start": "",
    "ai_time_range_end": "",
}


class AIReplySettingsService:
    """Stores AI reply settings within the XYAccount metadata JSON blob."""

    def __init__(self, session: AsyncSession):
        self.session = session

    def _extract_settings(self, account: XYAccount) -> dict:
        stored = (account.metadata_json or {}).get("ai_reply_settings") or {}
        payload = DEFAULT_AI_SETTINGS.copy()
        # 只对"空字符串等价于从未配置过"的连接参数字段，把 "" 视同未设置（None），
        # 回落到默认值（含 RunJobs 网关 key/地址）——历史/导入数据里这几个字段常见
        # 被存成 ""（从未配置过），若按 "" 字面值覆盖默认值，会把新的网关默认配置
        # 永久屏蔽掉。
        # 不能对整个字典一刀切：custom_prompts 存 "" 的真实含义是"用户主动清空了
        # 自定义人设"，是一次有意义的保存结果，不是"未配置"；如果把它也当成未设置、
        # 回落到 AI_PERSONA 默认人设，就会用平台默认人设静默覆盖用户上一次的选择，
        # 这段文案会真实影响 AI 代表卖家跟买家对话的语气和内容。
        # ai_time_range_start/end 的默认值本来就是 ""，覆盖与否结果相同，不受影响。
        # max_discount_percent 等数值字段即使脏数据存了字面 ""，也不受这个集合影响，
        # 仍走下面 int(payload.get(...) or 0) 的旧逻辑。
        _EMPTY_AS_UNSET = {"api_key", "base_url", "model_name"}
        payload.update({
            k: v for k, v in stored.items()
            if v is not None and not (k in _EMPTY_AS_UNSET and v == "")
        })
        # 这一行无条件覆盖 DEFAULT_AI_SETTINGS["ai_enabled"]（见上方该字段的注释）：
        # 真实账号是否开启 AI 回复只由这里、也就是 stored 里显式存过的
        # ai_enabled/enabled 决定，与网关 key 是否存在无关。
        payload["ai_enabled"] = read_ai_enabled(stored)
        payload["max_discount_percent"] = int(payload.get("max_discount_percent", 10) or 0)
        payload["max_discount_amount"] = int(payload.get("max_discount_amount", 100) or 0)
        payload["max_bargain_rounds"] = int(payload.get("max_bargain_rounds", 3) or 0)
        payload["provider_type"] = normalize_ai_provider_type(
            payload.get("provider_type"),
            payload.get("base_url"),
            payload.get("model_name"),
        )
        payload["model_name"] = clean_ai_text(payload.get("model_name"))
        payload["api_key"] = clean_ai_text(payload.get("api_key"))
        payload["base_url"] = clean_ai_text(payload.get("base_url"))
        payload["custom_prompts"] = payload.get("custom_prompts") or ""
        payload["ai_time_range_start"] = payload.get("ai_time_range_start") or ""
        payload["ai_time_range_end"] = payload.get("ai_time_range_end") or ""
        return payload

    async def get_settings(self, account: XYAccount) -> dict:
        return self._extract_settings(account)

    async def update_settings(self, account: XYAccount, payload: dict) -> dict:
        # 先获取现有设置，然后合并新的设置
        existing = self._extract_settings(account)
        
        # 只更新payload中明确提供的字段
        merged = existing.copy()
        if "ai_enabled" in payload:
            merged["ai_enabled"] = bool(payload.get("ai_enabled"))
        elif "enabled" in payload:
            merged["ai_enabled"] = bool(payload.get("enabled"))
        if "provider_type" in payload:
            merged["provider_type"] = normalize_ai_provider_type(
                payload.get("provider_type"),
                payload.get("base_url") or merged.get("base_url"),
                payload.get("model_name") or merged.get("model_name"),
            )
        if "model_name" in payload:
            merged["model_name"] = clean_ai_text(payload.get("model_name"))
        if "api_key" in payload:
            merged["api_key"] = clean_ai_text(payload.get("api_key"))
        if "base_url" in payload:
            merged["base_url"] = clean_ai_text(payload.get("base_url"))
        if "max_discount_percent" in payload:
            merged["max_discount_percent"] = int(payload.get("max_discount_percent", 10) or 0)
        if "max_discount_amount" in payload:
            merged["max_discount_amount"] = int(payload.get("max_discount_amount", 100) or 0)
        if "max_bargain_rounds" in payload:
            merged["max_bargain_rounds"] = int(payload.get("max_bargain_rounds", 3) or 0)
        if "custom_prompts" in payload:
            merged["custom_prompts"] = payload.get("custom_prompts") or ""
        if "ai_time_range_start" in payload:
            merged["ai_time_range_start"] = payload.get("ai_time_range_start") or ""
        if "ai_time_range_end" in payload:
            merged["ai_time_range_end"] = payload.get("ai_time_range_end") or ""
        merged["provider_type"] = normalize_ai_provider_type(
            merged.get("provider_type"),
            merged.get("base_url"),
            merged.get("model_name"),
        )
        if merged.get("ai_enabled"):
            missing_fields = get_ai_settings_missing_fields(merged)
            if missing_fields:
                raise ValueError(f"AI配置未填写完整，请先补全：{'、'.join(missing_fields)}")
        merged["enabled"] = merged["ai_enabled"]
        
        metadata = dict(account.metadata_json or {})
        metadata["ai_reply_settings"] = merged
        stmt = (
            update(XYAccount)
            .where(XYAccount.id == account.id)
            .values(metadata_json=metadata)
        )
        await self.session.execute(stmt)
        await self.session.commit()
        account.metadata_json = metadata
        return merged

    async def list_settings(self, owner_id: int) -> dict[str, dict]:
        stmt = select(XYAccount).where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        accounts = result.scalars().all()
        return {account.account_id: self._extract_settings(account) for account in accounts}
