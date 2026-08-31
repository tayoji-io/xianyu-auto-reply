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
  # -b 让 .pyc 与 .py 同名同目录（而非 __pycache__），便于直接运行
  "$PY" -m compileall -b -q "$ROOT/$svc" -x '(node_modules|__pycache__|\.venv)'
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
  # -c core.quotePath=false：git 默认会把含非 ASCII 字节（如中文文件名）的
  # 路径输出成带引号的八进制转义字符串（例如 "\345\201\234...bat"），字面
  # 传给 rsync --files-from 会导致 stat 失败——仓库里的 启动.bat/停止.bat
  # 就踩了这个坑，必须关掉引用才能拿到原始 UTF-8 文件名。
  filelist="$(mktemp)"
  git -c core.quotePath=false -C "$ROOT/$svc" ls-files | awk '$0 !~ /\.py$/' > "$filelist"
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

echo "==> 完成：$OUT"
du -sh "$OUT"/*
