"""针对 scripts/gen_requirements.py 依赖合并逻辑的单元测试。

背景：早期实现用纯字符串比较（'>=0.10.0' > '>=0.9.0' 按字典序为 False，
因为 '1' < '9'）挑选"更严格"的版本约束，会在版本号位数不同时选错。
这里改用 packaging 做语义化版本比较，并对不能安全判定优劣的场景
（环境标记不同、==/~= 等非单调操作符）保守地保留全部声明。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from gen_requirements import merge_dependencies, merge_group  # noqa: E402


def test_semantic_version_beats_string_order():
    """'>=0.10.0' 语义上严于 '>=0.9.0'，即便字符串比较会得出相反结论。"""
    result = merge_group(["foo>=0.9.0", "foo>=0.10.0"])
    assert result == ["foo>=0.10.0"]

    # 顺序颠倒结果应一致（不依赖遍历顺序）。
    result_reversed = merge_group(["foo>=0.10.0", "foo>=0.9.0"])
    assert result_reversed == ["foo>=0.10.0"]


def test_extras_are_not_dropped_when_merging():
    """合并时 extras 要取并集，不能因为选中了另一条约束就把 extras 弄丢。"""
    result = merge_group(["passlib[bcrypt]>=1.7.0", "passlib>=1.7.4"])
    assert result == ["passlib[bcrypt]>=1.7.4"]


def test_case_insensitive_same_package_not_duplicated():
    """大小写不同但同名的包（如 Pillow / pillow）合并后只应出现一行。"""
    lines = merge_dependencies(["Pillow>=10.0.0", "pillow>=10.0.0"])
    assert lines == ["pillow>=10.0.0"]


def test_identical_duplicate_lines_collapse_to_one():
    result = merge_group(["uvicorn[standard]>=0.24.0", "uvicorn[standard]>=0.24.0"])
    assert result == ["uvicorn[standard]>=0.24.0"]


def test_incomparable_operator_keeps_all_constraints():
    """== 这类非单调操作符版本不同即可能是真实冲突，不能静默挑一个。"""
    result = merge_group(["websockets==12.0", "websockets==11.0"])
    assert set(result) == {"websockets==12.0", "websockets==11.0"}


def test_different_environment_markers_keep_all_constraints():
    """环境标记不同代表适用条件不同，语义不等价，不能合并成一条。"""
    specs = [
        "pyautogui>=0.9.54; sys_platform == 'win32'",
        "pyautogui>=0.9.50",
    ]
    result = merge_group(specs)
    assert set(result) == set(specs)


def test_bare_requirement_yields_to_versioned_one():
    """裸依赖（无版本约束）不构成真实冲突，应该被有版本约束的一条覆盖。"""
    result = merge_group(["playwright", "playwright>=1.40.0"])
    assert result == ["playwright>=1.40.0"]


def test_merge_dependencies_sorts_by_canonical_name():
    lines = merge_dependencies(["zeta>=1.0", "Alpha>=2.0", "alpha>=1.0"])
    assert lines == ["alpha>=2.0", "zeta>=1.0"]
