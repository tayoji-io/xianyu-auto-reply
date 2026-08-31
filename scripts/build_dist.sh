#!/usr/bin/env bash
# scripts/build_dist.sh — 产出 RunJobs 发布仓布局
# 用法: ./scripts/build_dist.sh [输出目录]  默认 dist_out
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist_out}"
SERVICES=(backend-web websocket scheduler common)

# 编译 .pyc 必须使用与目标沙盒同一 Python 大版本（3.11）的解释器，
# 否则产出的 .pyc 魔数不匹配，沙盒无法加载。优先使用本地开发用的
# .venv311；找不到时回退到 python3（要求调用方自行确保其版本正确）。
PY="${PY:-$ROOT/.venv311/bin/python}"
if [ ! -x "$PY" ]; then
  echo "警告：未找到 $PY，回退使用 python3（请确认其版本与目标沙盒一致）" >&2
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
  # 只复制 .pyc 与非 Python 资源，不带 .py 源码
  rsync -a --prune-empty-dirs \
    --include='*/' --include='*.pyc' --include='*.json' --include='*.yaml' \
    --include='*.yml' --include='*.sql' --include='*.html' \
    --exclude='*' "$ROOT/$svc/" "$OUT/app/$svc/"
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
