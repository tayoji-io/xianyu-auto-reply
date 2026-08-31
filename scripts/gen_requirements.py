# scripts/gen_requirements.py
"""汇总 backend-web / websocket / scheduler 三个服务的 pyproject 依赖为单一 requirements.txt。

三服务在同一沙盒内共用一个 venv，依赖取并集；同名依赖保留版本约束最严格的一条
（简单策略：按字符串排序取最后一条，因为 '>=0.25.0' > '>=0.2.9'）。
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

SERVICES = ["backend-web", "websocket", "scheduler"]
ROOT = Path(__file__).resolve().parent.parent


def pkg_name(spec: str) -> str:
    """从 'fastapi>=0.104.0' 或 'passlib[bcrypt]>=1.7.4' 提取 'passlib'。"""
    return re.split(r"[\[><=!~;]", spec, 1)[0].strip().lower()


def main() -> int:
    merged: dict[str, str] = {}
    for svc in SERVICES:
        path = ROOT / svc / "pyproject.toml"
        if not path.exists():
            print(f"缺少 {path}", file=sys.stderr)
            return 1
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for spec in data["project"]["dependencies"]:
            name = pkg_name(spec)
            if name not in merged or spec > merged[name]:
                merged[name] = spec

    lines = [merged[k] for k in sorted(merged)]
    out = ROOT / "requirements.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写入 {out}，共 {len(lines)} 个依赖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
