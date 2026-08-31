# provision.py — RunJobs 初始化脚本（粘贴到发布页「初始化脚本」）
# 注意：create_port_preview 之前项目页不显示任何输出，故全部日志写文件。
import os
import subprocess
import time
from pathlib import Path

WORKSPACE = Path("/home/user/workspace")
APP_DIR = WORKSPACE / "app"
CONF_DIR = WORKSPACE / "conf"
DATA_DIR = WORKSPACE / "data"
LOG_DIR = WORKSPACE / "logs"
STAGE_FILE = WORKSPACE / ".stage"
REPO = "https://github.com/jiyota-dev/runjobs_xianyu.git"  # 公开仓，沙盒 clone 无需凭据

for d in (CONF_DIR, DATA_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with (LOG_DIR / "init.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _done() -> set[str]:
    if not STAGE_FILE.exists():
        return set()
    return set(STAGE_FILE.read_text(encoding="utf-8").split())


def stage(name: str) -> bool:
    """该阶段是否需要执行。"""
    if name in _done():
        log(f"跳过阶段 {name}（已完成）")
        return False
    return True


def mark(name: str) -> None:
    with STAGE_FILE.open("a", encoding="utf-8") as f:
        f.write(name + "\n")
    log(f"阶段 {name} 完成")


def run(cmd: str, check: bool = True) -> str:
    log(f"$ {cmd}")
    p = subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=1800,
    )
    out = p.stdout or ""
    if out.strip():
        log(out[-4000:])
    if check and p.returncode != 0:
        raise RuntimeError(f"命令失败（exit {p.returncode}）: {cmd}")
    return out


def fail(msg: str) -> None:
    log(f"!! 失败: {msg}")
    (LOG_DIR / "init.err").write_text(msg, encoding="utf-8")
    raise SystemExit(1)


log("=== 初始化开始 ===")
# 记录执行环境：Task 11 依赖本解释器能 import passlib（/opt/venv 的 site-packages）
import sys as _sys
log(f"解释器: {_sys.executable}")
log(f"sys.path 首项: {_sys.path[:3]}")
try:
    import passlib  # noqa: F401
    log("passlib 可用 ✓")
except ImportError:
    log("!! passlib 不可用 —— Task 11 的密码覆盖将失败，需改用 /opt/venv/bin/python3 子进程执行")
# Task 9-12 的阶段将在此处依次追加
log("=== 骨架就绪 ===")
