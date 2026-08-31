#!/usr/bin/env bash
# scripts/build_dist.sh — 产出 RunJobs 发布仓布局
# 用法: ./scripts/build_dist.sh [输出目录]  默认 dist_out
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist_out}"
# 规范化 $OUT 为绝对路径：不加规范化的话，传相对路径时会按“调用者当时的
# cwd”解析（而非脚本所在目录），容易在后续 rm -rf 时指向意外位置。若父目录
# 不存在则直接报错退出，不做任何猜测。
if [[ "$OUT" != /* ]]; then
  OUT="$PWD/$OUT"
fi
OUT_PARENT="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd)" || {
  echo "错误：输出目录的父目录不存在: $(dirname "$OUT")" >&2
  exit 1
}
OUT="$OUT_PARENT/$(basename "$OUT")"
SERVICES=(backend-web websocket scheduler common)

# 编译 .pyc 必须使用与目标沙盒同一 Python 大版本（3.11）的解释器，
# 否则产出的 .pyc 魔数不匹配，沙盒无法加载。优先使用本地开发用的
# .venv311；找不到时回退到 python3（要求调用方自行确保其版本正确）。
PY="${PY:-$ROOT/.venv311/bin/python}"
if [ ! -x "$PY" ]; then
  echo "警告：未找到 ${PY}，回退使用 python3（请确认其版本与目标沙盒一致）" >&2
  PY="python3"
fi
echo "==> 使用解释器: $PY ($("$PY" --version 2>&1))"

echo "==> 清理 $OUT"
rm -rf "$OUT"
mkdir -p "$OUT/app" "$OUT/web"

echo "==> 汇总依赖"
"$PY" "$ROOT/scripts/gen_requirements.py"
cp "$ROOT/requirements.txt" "$OUT/requirements.txt"

echo "==> 编译 .pyc"
for svc in "${SERVICES[@]}"; do
  # -b 让 .pyc 与 .py 同名同目录（而非 __pycache__），便于直接运行。
  # -s "$ROOT" -p "app"：.pyc 里的 co_filename 默认写编译时的绝对路径
  # （traceback 报错时会原样打印出来），一旦推到公开仓，每个订阅用户的
  # 沙盒 traceback、以及直接 `strings *.pyc | grep Volumes` 都能看到
  # 维护者本机的用户名/卷布局/仓库路径。-s 去掉 "$ROOT" 这段源码根前缀，
  # -p 换成产物内的相对前缀 "app"，效果是 co_filename 从
  # "/Volumes/.../xianyu-runjobs-port/common/xxx.py" 变成
  # "app/common/xxx.py"——和产物里 .pyc 实际所在的相对路径一致，traceback
  # 依然指向正确文件，只是不再暴露编译机的绝对路径。
  "$PY" -m compileall -b -q -s "$ROOT" -p "app" "$ROOT/$svc" -x '(node_modules|__pycache__|\.venv)'
  mkdir -p "$OUT/app/$svc"

  # 打包清单 = 两部分合并：
  #   1) 资源文件：git 追踪的文件（排除 .py 源码）。以 git 为准而非手写扩展名
  #      白名单，新增资源类型不会被静默漏掉；同时天然排除本地脏文件——例如
  #      被 .gitignore 挡住的 backend-web/logs/*.log、pytest/mypy 残留缓存等，
  #      这些永远不会出现在 git ls-files 里。
  #   2) 代码文件：compileall 生成的 legacy .pyc（与 .py 同名同目录），.pyc
  #      本身不受版本控制，必须单独收集；显式排除 __pycache__ 里的陈旧缓存。
  # 已核实四个服务目录下不存在 .pyi/.pyx/.pyd 等 Python 变体文件，排除 *.py
  # 足以完全拦截源码。
  # 额外排除 .env.example：.gitignore 里有 `!**/.env.example` 的显式反排除
  # 规则（作者有意让示例配置进源码仓，方便部署时复制成 .env），所以
  # git ls-files 会列出它——但这份发布仓是公开的、会被每个订阅用户 clone，
  # 而 .env.example 对沙盒运行毫无用处（初始化脚本会生成真正的 .env）。
  # backend-web/.env.example 里的 EXTERNAL_API_KEY=zhinian_bk 是具体值而非
  # your_xxx/change-me 这类占位符，打包出去等于让所有沙盒默认共用同一个
  # 指向原作者服务器的第三方密钥，必须排除。
  # 额外排除 Dockerfile/pyproject.toml/*.bat：沙盒不会执行 docker build，
  # 也不会跑 Windows 批处理，这三类文件对沙盒运行纯属冗余（依赖信息已经
  # 冗余存在于顶层合并生成的 requirements.txt 里）；顺带消除了下面凭据扫描
  # 的一个盲区主要实际载体——Dockerfile 的 `ENV KEY=value` 是最常见的手滑
  # 写死凭据的位置之一，不打包这几个文件就不必依赖扫描器兜底。
  # -c core.quotePath=false：git 默认会把含非 ASCII 字节（如中文文件名）的
  # 路径输出成带引号的八进制转义字符串（例如 "\345\201\234...bat"），字面
  # 传给 rsync --files-from 会导致 stat 失败——仓库里的 启动.bat/停止.bat
  # 就踩了这个坑（虽然 .bat 现在也被排除了，但这个转义问题对任何非 ASCII
  # 文件名都成立，仍然需要关掉引用才能让下面的 awk 排除规则按字面文本匹配）。
  filelist="$(mktemp)"
  git -c core.quotePath=false -C "$ROOT/$svc" ls-files \
    | awk '$0 !~ /\.py$/ \
        && $0 !~ /(^|\/)\.env\.example$/ \
        && $0 !~ /(^|\/)Dockerfile$/ \
        && $0 !~ /(^|\/)pyproject\.toml$/ \
        && $0 !~ /\.bat$/' > "$filelist"
  while IFS= read -r pyc; do
    printf '%s\n' "${pyc#"$ROOT/$svc/"}" >> "$filelist"
  done < <(find "$ROOT/$svc" -name '*.pyc' -not -path '*/__pycache__/*')

  rsync -a --files-from="$filelist" "$ROOT/$svc/" "$OUT/app/$svc/"
  rm -f "$filelist"
done

echo "==> 构建前端"
cd "$ROOT/frontend"
npm ci
npm run build
rsync -a "$ROOT/frontend/dist/" "$OUT/web/"

echo "==> 清理源码仓内 compileall 留下的 .pyc（避免污染工作区）"
for svc in "${SERVICES[@]}"; do
  find "$ROOT/$svc" -name '*.pyc' -not -path '*/__pycache__/*' -delete
done

echo "==> 写版本号"
git -C "$ROOT" rev-parse --short HEAD > "$OUT/VERSION"

echo "==> 校验：产物中不应存在 .py 源码"
if find "$OUT/app" -name '*.py' | grep -q .; then
  echo "错误：产物包含 .py 源码" >&2
  find "$OUT/app" -name '*.py' >&2
  exit 1
fi

echo "==> 脱敏：清理 human_trails 采集样本里残留的真实会话数据（只处理产物副本，不动源码仓）"
"$PY" - "$OUT" <<'SANITIZEEOF'
# human_trails/*.json 是验证码/登录滑块的真人轨迹采集样本，采集时顺手落盘
# 了浏览器真实 cookies、带 token 的登录/滑块验证链接——这些是采集副产品，
# common/services/captcha/real_mouse_slider.py::_load_drags() 只读取
# trail/passed（业务场景另外还读 slide_code/slider_distance，均不在剔除
# 名单内），功能上完全用不到，却是真实账号的会话数据，绝不能随公开产物
# 分发给订阅用户。
#
# 按字段名剔除、不按文件名做区分：不只是 4 个 human_trail_login_pass_*.json
# 带完整登录 cookies，14 个 human_trail_pass_*.json（业务滑块样本）虽然没有
# cookies 字段，但同样带了含真实 x5secdata token 的 frame_url——统一按这
# 5 个字段名处理，对全部 18 个样本一视同仁，不依赖"哪种文件名才需要脱敏"
# 这种容易漏判的假设。
import glob
import json
import os
import sys

OUT = sys.argv[1]
TRAILS_DIR = os.path.join(OUT, "app", "common", "services", "captcha", "human_trails")

SENSITIVE_FIELDS = ("cookies", "login_url", "slider_url", "frame_url", "login_response")

if os.path.isdir(TRAILS_DIR):
    for path in sorted(glob.glob(os.path.join(TRAILS_DIR, "*.json"))):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            continue
        removed = [k for k in SENSITIVE_FIELDS if k in data]
        if not removed:
            continue
        for k in SENSITIVE_FIELDS:
            data.pop(k, None)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        print("  已脱敏: " + os.path.relpath(path, OUT) + "（剔除字段: " + ", ".join(removed) + "）")
SANITIZEEOF

echo "==> 校验：产物中不应包含疑似凭据（这份发布仓是公开的，会被每个订阅用户 clone）"
"$PY" - "$OUT" <<'CREDSCANEOF'
# 扫描 $OUT 全树，命中即报错退出（不是警告）。分两类检查：
#
# 1) 文件名黑名单（扫全树 app/ + web/ + 顶层文件）：任何 .env 变体（.env、
#    .env.example、.env.local、.env.production……）、私钥文件（.pem/.key/
#    id_rsa/id_rsa.pub）。.env.example 已在打包阶段被显式排除，这里是纵深
#    防御——万一以后打包逻辑改动又漏排除，构建仍会在这一步失败，而不是静默
#    把它推到公开仓。
#
# 2) 内容扫描（只扫 app/ 下的资源文件 + 顶层 requirements.txt，跳过 .pyc 和
#    图片等二进制——.pyc 是编译产物不是手改配置，图片扫文本没有意义，这两类
#    不是这次要防的"配置文件里手滑写死凭据"这个场景）：逐行匹配形如
#    `XXX_API_KEY=真实值` / `XXX_SECRET: 真实值` 的赋值，键名要求以
#    API_KEY/SECRET(_KEY)?/PASSWORD/PASSWD/PWD/ACCESS_KEY/PRIVATE_KEY/TOKEN/
#    CLIENT_SECRET 结尾（后缀匹配而非子串匹配，避免 ACCESS_TOKEN_EXPIRE_
#    MINUTES=30、TOKEN_REFRESH_INTERVAL=72000 这类名字里含 TOKEN 但实际是
#    数值配置的行被误报）。行首允许可选的 `ENV `/`export ` 前缀（大小写不
#    敏感），覆盖 Dockerfile 的 `ENV KEY=value` 和 shell 脚本的
#    `export KEY=value` 写法——这两种是"手滑写死凭据"最常见的落地位置，
#    只按裸 `KEY=value` 匹配会整行跳过（`ENV` 被误当成键名，`ENV` 后面是
#    空格不是 `=`/`:`，正则直接不匹配）。
#
#    另外扫描 JSON 结构里的 `"key": value`（JSON 文件通常压缩成一整行，
#    用 finditer 找一行内出现的所有键，而不是只看行首）：
#      a) 值为字符串且键名匹配上面同一套 SENSITIVE_KEY_SUFFIX 时，走同样
#         的占位符判定（覆盖 `{"api_key": "sk-real"}` 这类通用 JSON 凭据）；
#      b) 键名命中一小撮"结构性敏感"字段名（cookies/login_response/
#         slider_url/frame_url/login_url）时无条件报错，不做占位符豁免——
#         这几个字段不存在"合法占位符"，值是数组/对象也一样命中（例如
#         `"cookies": [...]`）。这是从一次真实泄露（human_trails/*.json
#         采集样本残留的完整登录会话 cookies + 带 token 的验证链接）里补的
#         教训，脚本已经在下面把这些字段从产物副本里脱敏掉，这里是纵深
#         防御：万一脱敏步骤以后被改坏，构建仍会在这一步失败。
#
# 占位符判定标准（命中即视为"看起来本来就是留给用户填的示例"，不报错）：
#   - 以 your_/your-/change-me/change_me/changeme/xxx/*** 开头
#   - 是模板变量：<...>、${...}、{{...}}
#   - 含 example.com / localhost 这类明显占位域名
#   - 中文占位提示：示例、占位、请填写、请替换
#   - 值为空
# 这些标准是本次为该项目量身定的启发式判断，不是通用密钥检测器；命中以外
# 的、看起来像真实值的赋值一律报错，交给人工复核，宁可误报也不漏报。
import os
import re
import sys

OUT = sys.argv[1]

ENV_NAME_RE = re.compile(r'^\.env(\..*)?$', re.IGNORECASE)
KEYFILE_EXT = ('.pem', '.key')
KEYFILE_NAME = ('id_rsa', 'id_rsa.pub')

SENSITIVE_KEY_SUFFIX = re.compile(
    r'(API[_-]?KEY|SECRET(?:[_-]?KEY)?|PASSWORD|PASSWD|PWD|'
    r'ACCESS[_-]?KEY|PRIVATE[_-]?KEY|TOKEN|CLIENT[_-]?SECRET)$',
    re.IGNORECASE,
)
# 可选前缀 `ENV `/`export `（Dockerfile / shell 写法）+ 裸 `KEY=value`
ASSIGN_RE = re.compile(
    r'^(?:(?:env|export)\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*[:=]\s*(.+)$',
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(
    r'^(your[_-]|change[_-]?me|xxx+|\*{3,}|redact|replace|todo'
    r'|<.*>|\$\{.*\}|\{\{.*\}\}'
    r'|示例|占位|请填写|请替换)',
    re.IGNORECASE,
)
PLACEHOLDER_DOMAIN_RE = re.compile(r'(example\.(com|org|net)|localhost|127\.0\.0\.1)', re.IGNORECASE)
CONTENT_SKIP_EXT = {
    '.pyc', '.jpg', '.jpeg', '.png', '.gif', '.ico', '.bmp',
    '.woff', '.woff2', '.ttf', '.eot',
}

# JSON 结构：`"key":` 后面跟字符串/数组/对象都能匹配（只捕获键名，值类型
# 交给调用处按需再判断），配合 finditer 在一整行里找出全部出现的键。
JSON_KEY_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_.\-]*)"\s*:\s*')
# 值恰好是引号字符串时，用这个从紧跟键名之后的文本里把该字符串取出来。
JSON_STRING_VALUE_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"')
# 无条件报错、不做占位符豁免的 JSON 字段名（小写比较）。
JSON_STRUCTURAL_SENSITIVE_KEYS = {
    'cookies', 'cookie', 'login_response', 'slider_url', 'frame_url', 'login_url',
}

findings = []

for root, dirs, files in os.walk(OUT):
    for name in files:
        path = os.path.join(root, name)
        rel = os.path.relpath(path, OUT)
        lower = name.lower()

        # 1) 文件名黑名单：全树都查
        if ENV_NAME_RE.match(name) or lower.endswith(KEYFILE_EXT) or lower in KEYFILE_NAME:
            findings.append("文件名疑似凭据/密钥文件: " + rel)
            continue

        # 2) 内容扫描：只看 app/ 下的资源文件 + 顶层 requirements.txt
        in_app = rel.startswith('app' + os.sep)
        is_top_requirements = rel == 'requirements.txt'
        if not (in_app or is_top_requirements):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in CONTENT_SKIP_EXT:
            continue

        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
                for lineno, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith('#') or stripped.startswith('//'):
                        continue

                    # a) 裸 KEY=value / KEY: value（.env、Dockerfile ENV、shell export……）
                    m = ASSIGN_RE.match(stripped)
                    if m:
                        key, value = m.group(1), m.group(2).strip().strip('\'"')
                        if (
                            value
                            and SENSITIVE_KEY_SUFFIX.search(key)
                            and not PLACEHOLDER_RE.search(value)
                            and not PLACEHOLDER_DOMAIN_RE.search(value)
                        ):
                            findings.append(rel + ":" + str(lineno) + ": 疑似真实凭据（非占位符）: " + key + "=...")

                    # b) JSON 结构：一行内可能出现多个 "key": value，逐个检查
                    for jm in JSON_KEY_RE.finditer(stripped):
                        jkey = jm.group(1)
                        jkey_lower = jkey.lower()
                        if jkey_lower in JSON_STRUCTURAL_SENSITIVE_KEYS:
                            findings.append(
                                rel + ":" + str(lineno)
                                + ': JSON 字段疑似携带真实会话数据（结构性敏感字段）: "' + jkey + '"'
                            )
                            continue
                        if not SENSITIVE_KEY_SUFFIX.search(jkey):
                            continue
                        rest = stripped[jm.end():]
                        vm = JSON_STRING_VALUE_RE.match(rest)
                        if not vm:
                            continue  # 值不是引号字符串（数组/对象/数字/布尔），上面的结构性检查已经兜底
                        jvalue = vm.group(1)
                        if not jvalue:
                            continue
                        if PLACEHOLDER_RE.search(jvalue) or PLACEHOLDER_DOMAIN_RE.search(jvalue):
                            continue
                        findings.append(
                            rel + ":" + str(lineno)
                            + ': JSON 疑似真实凭据（非占位符）: "' + jkey + '": ...'
                        )
        except (OSError, UnicodeDecodeError):
            continue

if findings:
    sys.stderr.write("错误：产物中检测到疑似凭据，构建中止：\n")
    for item in findings:
        sys.stderr.write("  - " + item + "\n")
    sys.exit(1)

print("凭据扫描通过：未发现疑似凭据")
CREDSCANEOF

echo "==> 完成：$OUT"
du -sh "$OUT"/*
