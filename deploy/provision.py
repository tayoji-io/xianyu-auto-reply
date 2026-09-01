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


def run(cmd: str, check: bool = True, timeout: int = 1800) -> str:
    """执行 shell 命令。返回 stdout+stderr 合并输出。

    注意 shell=True：**绝不要把外部内容（密码、哈希、用户输入）直接拼进 cmd**，
    bash 会展开其中的 $ 与反引号。SQL 一律走 run_sql()。
    """
    log(f"$ {cmd}")
    try:
        p = subprocess.run(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
        out, rc = p.stdout or "", p.returncode
    except subprocess.TimeoutExpired as e:
        # 超时同样要落盘已产生的输出 —— apt/pip 这类长命令最需要现场
        raw = e.output or ""
        out = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        log(f"!! 超时（{timeout}s），已捕获输出：")
        if out.strip():
            log(out[-4000:])
        raise
    if out.strip():
        log(out[-4000:])
    log(f"  exit={rc}")
    if check and rc != 0:
        raise RuntimeError(f"命令失败（exit {rc}）: {cmd}")
    return out


def sql_quote(v: str) -> str:
    """SQL 字符串字面量转义（反斜杠与单引号）。只用于值，不用于标识符。"""
    return v.replace("\\", "\\\\").replace("'", "''")


def run_sql(sql: str, db: str = "", raw: bool = False, check: bool = True) -> str:
    """执行 SQL：写临时文件后重定向输入，**不把 SQL 拼进 shell 字符串**。

    这不是洁癖 —— passlib 的 pbkdf2_sha256 哈希形如 $pbkdf2-sha256$29000$salt$hash，
    直接拼进 shell 双引号会被 bash 展开成 "-sha2569000"，写进库的密码永远登录不上，
    而 UPDATE 仍返回成功、日志毫无异常。
    """
    f = CONF_DIR / ".tmp.sql"
    f.write_text(sql, encoding="utf-8")
    try:
        flags = "-N -B " if raw else ""
        return run(
            f"mariadb --socket={DATA_DIR}/mysql.sock -u root {flags}{db} < {f}",
            check=check,
        )
    finally:
        f.unlink(missing_ok=True)


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

# ---- 阶段 apt：系统依赖 ----
if stage("apt"):
    run("sudo apt-get update -qq")
    # asyncmy 0.2.14 有预编译 wheel（Task 1 实测），无需编译工具链
    run(
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "mariadb-server redis-server supervisor"
    )
    # 平台以容器方式运行，禁用发行版自带的服务管理，统一交给 supervisor
    run("sudo systemctl disable mariadb redis-server 2>/dev/null || true", check=False)
    # 注意：不能用 `pkill -f mariadbd`——run() 经 shell=True 执行，
    # 实际调用的是 `sh -c "sudo pkill -f mariadbd || true"`，这条 sh 进程自身的
    # 完整命令行里就含有 "mariadbd" 字样，会被 -f（按完整命令行匹配）连带命中并杀掉
    # 执行它自己的 shell，导致 `|| true` 因宿主 shell 已死而永远不会被求值
    # （退出码从而变成不可信的 -15）。改用 -x 按精确进程名（comm，不含参数）匹配，
    # 不会命中调用它的 sh/sudo 进程。
    run("sudo pkill -x mariadbd || true", check=False)
    run("sudo pkill -x redis-server || true", check=False)
    mark("apt")

# ---- 阶段 datadir：数据目录与配置文件 ----
# 注意：my.cnf / redis.conf 用 write_text 整段覆写，且只在本阶段"首次"执行时写一次
# （幂等由 stage()/mark() 保证）。以后若要调整 buffer pool、max_connections、
# maxmemory 等参数，必须先手动删除 ~/workspace/.stage 里的 "datadir" 这一行，
# 否则 stage("datadir") 会直接返回 False、配置文件不会被重新生成，
# 光改这里的数值代码不会在已初始化过的沙盒里生效。
if stage("datadir"):
    mysql_dir = DATA_DIR / "mysql"
    redis_dir = DATA_DIR / "redis"
    mysql_dir.mkdir(parents=True, exist_ok=True)
    redis_dir.mkdir(parents=True, exist_ok=True)

    # 4G 内存下的保守参数（原 compose 为 300 连接 / 256M，此处下调）
    (CONF_DIR / "my.cnf").write_text(f"""[mysqld]
user=user
datadir={mysql_dir}
socket={DATA_DIR}/mysql.sock
pid-file={DATA_DIR}/mysql.pid
bind-address=127.0.0.1
port=3306
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
default-time-zone='+08:00'
innodb_buffer_pool_size=128M
max_connections=50
max_allowed_packet=64M
skip-name-resolve

[client]
socket={DATA_DIR}/mysql.sock
""", encoding="utf-8")

    (CONF_DIR / "redis.conf").write_text(f"""bind 127.0.0.1
port 6379
dir {redis_dir}
maxmemory 64mb
maxmemory-policy allkeys-lru
appendonly yes
save ""
""", encoding="utf-8")

    # 初始化 mariadb 数据目录（幂等：已存在 mysql 系统库则跳过）
    if not (mysql_dir / "mysql").exists():
        run(f"mariadb-install-db --user=user --datadir={mysql_dir} --auth-root-authentication-method=normal")
    mark("datadir")

log("=== 骨架就绪 ===")

# ---- 每次执行：拉取运行时产物（首次 clone，之后 pull 用于升级）----
if (APP_DIR / ".git").exists():
    run(f"cd {APP_DIR} && git pull --ff-only", check=False)
else:
    run(f"git clone --depth 1 {REPO} {APP_DIR}")
log(f"代码版本: {(APP_DIR / 'VERSION').read_text().strip()}")

# ---- 阶段 pydeps：Python 依赖 ----
# 两个坑（Task 1 实测）：
#   1. /opt/venv/lib/python3.11/site-packages 属主为 root，普通用户装不进去，必须 sudo
#   2. -E 不可省 —— 丢失 VIRTUAL_ENV 后 uv 直接报错
#   3. pip 是 uv pip 的壳，不接受 --break-system-packages
if stage("pydeps"):
    run(f"sudo -E pip install -r {APP_DIR}/requirements.txt")
    mark("pydeps")

# ---- 阶段 browser：Chromium ----
# 项目用 patchright（反检测版），与沙盒预装的 playwright 1.58 浏览器共存不冲突；
# 新增约 651MB，~/.cache/ms-playwright 总占用约 1.3GB
if stage("browser"):
    run("sudo -E /opt/venv/bin/python3 -m patchright install-deps chromium")
    run("/opt/venv/bin/python3 -m patchright install chromium")
    mark("browser")

# ---- 每次执行：写 .env ----
admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
if not admin_password:
    fail("引导表单未提供 ADMIN_PASSWORD")

(APP_DIR / ".env").write_text(f"""ENVIRONMENT=production
TZ=Asia/Shanghai
HOST=0.0.0.0
BACKEND_WEB_PORT=8089
WEBSOCKET_PORT=8090
SCHEDULER_PORT=8091
BACKEND_WEB_SERVICE_URL=http://localhost:8089
WEBSOCKET_SERVICE_URL=http://localhost:8090
SCHEDULER_SERVICE_URL=http://localhost:8091
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=xianyu
MYSQL_PASSWORD={os.getenv('DB_PASSWORD', 'xianyu-local-only')}
MYSQL_DATABASE=xianyu_data
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
STATIC_DIR={APP_DIR}/app/backend-web/static
WEB_DIR={APP_DIR}/web
BROWSER_HEADLESS=true
MAX_CAPTCHA_CONCURRENT=1
MAX_BROWSER_CONCURRENT=2
LOG_LEVEL=INFO
""", encoding="utf-8")
log(".env 已写入")

# ---- 阶段 supervisor：拉起全部进程 ----
if stage("supervisor"):
    run(f"sudo cp {APP_DIR}/supervisord.conf /etc/supervisord.conf")
    mark("supervisor")

# 每次执行：确保 supervisor 在运行
# 注意：不能用 `pgrep -f supervisord`——沙盒基础镜像自身有一个 PID 1 的
# supervisord（/usr/bin/python3 /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf，
# 管理平台侧基础设施，与本应用无关），命令行里同样含有 "supervisord" 字样，
# 会被 -f（按完整命令行匹配）恒定命中，导致 `running` 恒为真、
# 我们自己这份 -c /etc/supervisord.conf 的 supervisord 永远不会被首次拉起。
# 改用只匹配我们自己配置文件绝对路径的模式，platform 那份路径是
# /etc/supervisor/conf.d/supervisord.conf，不含 "/etc/supervisord.conf" 子串，不会误命中。
running = run("pgrep -f '/etc/supervisord.conf' || true", check=False).strip()
if running:
    run("sudo supervisorctl -c /etc/supervisord.conf reread", check=False)
    run("sudo supervisorctl -c /etc/supervisord.conf update", check=False)
    run("sudo supervisorctl -c /etc/supervisord.conf restart all", check=False)
else:
    log(f"启动 supervisord: {call_tool('exec', command='sudo supervisord -c /etc/supervisord.conf', detach=True)}")
log("supervisor 已启动")
