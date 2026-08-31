# scripts/gen_requirements.py
"""汇总 backend-web / websocket / scheduler 三个服务的 pyproject 依赖为单一 requirements.txt。

三服务在同一沙盒内共用一个 venv，依赖取并集；同名依赖尽量用语义化版本比较
（`packaging` 库）选出更严格的约束（例如 '>=0.10.0' 严于 '>=0.9.0'，纯字符串
比较会得出相反结论）。当约束无法安全判定谁更严格时——环境标记不同、存在多个
版本子句、或操作符不是单调的 >=/>/<=/<（例如 ==、~=）——保守地把原始声明全部
保留，交给 pip/uv 在安装时自行求交集，绝不静默丢弃任何一条。
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import Specifier
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

SERVICES = ["backend-web", "websocket", "scheduler"]
ROOT = Path(__file__).resolve().parent.parent

# 只有这些操作符具备单调的"谁更严格"语义（上界越小/下界越大越严格）。
# ==、~=、!= 等不在其中：版本不同即视为潜在真实冲突，不做静默取舍。
_ORDERED_OPERATORS = {">=", ">", "<=", "<"}


def _stricter(a: Specifier, b: Specifier) -> Specifier | None:
    """当 a、b 是同一单调操作符的版本约束时，返回更严格的一个；否则返回 None。"""
    if a.operator != b.operator or a.operator not in _ORDERED_OPERATORS:
        return None
    try:
        va, vb = Version(a.version), Version(b.version)
    except InvalidVersion:
        return None
    if a.operator in (">=", ">"):
        return a if va >= vb else b
    return a if va <= vb else b  # "<=" / "<"：上限越小越严格


def merge_group(raw_specs: list[str]) -> list[str]:
    """合并同一个包（大小写不敏感）下收集到的所有原始依赖声明。

    - 完全相同的声明直接去重。
    - 剩余声明若环境标记一致、且每条最多只带一个可比较操作符的版本子句，
      用语义化版本比较选出更严格的一条，extras 取并集后重建。
    - 否则视为无法安全合并，原样保留全部声明（不吞掉任何一条）。
    """
    unique_raw = list(dict.fromkeys(raw_specs))
    if len(unique_raw) == 1:
        return unique_raw

    reqs = [Requirement(s) for s in unique_raw]
    markers = {str(r.marker) for r in reqs}
    spec_lists = [list(r.specifier) for r in reqs]
    operators = {sl[0].operator for sl in spec_lists if sl}

    can_merge = (
        len(markers) == 1
        and all(len(sl) <= 1 for sl in spec_lists)
        and len(operators) <= 1
        and operators <= _ORDERED_OPERATORS
    )
    if not can_merge:
        return unique_raw

    winner_spec: Specifier | None = None
    for sl in spec_lists:
        if not sl:
            continue
        if winner_spec is None:
            winner_spec = sl[0]
            continue
        picked = _stricter(winner_spec, sl[0])
        if picked is None:
            return unique_raw  # 防御性兜底：理论上不会触发（已被 can_merge 过滤）
        winner_spec = picked

    extras: set[str] = set()
    for r in reqs:
        extras |= r.extras

    name = canonicalize_name(reqs[0].name)
    extras_part = f"[{','.join(sorted(extras))}]" if extras else ""
    spec_part = str(winner_spec) if winner_spec is not None else ""
    marker_part = f"; {reqs[0].marker}" if reqs[0].marker else ""
    return [f"{name}{extras_part}{spec_part}{marker_part}"]


def merge_dependencies(specs: list[str]) -> list[str]:
    """把任意多条 PEP 508 依赖声明合并、按包名排序，返回最终应写入的行列表。"""
    grouped: dict[str, list[str]] = {}
    for spec in specs:
        name = canonicalize_name(Requirement(spec).name)
        grouped.setdefault(name, []).append(spec)

    lines: list[str] = []
    for name in sorted(grouped):
        lines.extend(merge_group(grouped[name]))
    return lines


def main() -> int:
    all_specs: list[str] = []
    for svc in SERVICES:
        path = ROOT / svc / "pyproject.toml"
        if not path.exists():
            print(f"缺少 {path}", file=sys.stderr)
            return 1
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        all_specs.extend(data["project"]["dependencies"])

    lines = merge_dependencies(all_specs)
    out = ROOT / "requirements.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写入 {out}，共 {len(lines)} 个依赖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
