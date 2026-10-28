#!/usr/bin/env bash
# 找一个装好依赖的 Python 来跑 merge.py
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

candidates=()
if [ -n "${DOCMERGE_PYTHON:-}" ]; then
  candidates+=("$DOCMERGE_PYTHON")
fi
candidates+=("$HOME/.workbuddy/binaries/python/envs/default/bin/python")
for name in python3 python; do
  path="$(command -v "$name" 2>/dev/null || true)"
  [ -n "$path" ] && candidates+=("$path")
done

for py in "${candidates[@]}"; do
  [ -x "$py" ] || continue
  if "$py" - <<'PY' >/dev/null 2>&1
import docx, pymupdf, bs4, lxml  # noqa: F401
PY
  then
    exec "$py" "$HERE/merge.py" "$@"
  fi
done

echo "找不到装好依赖的 Python（需要 python-docx / pymupdf / beautifulsoup4 / lxml）" >&2
echo "安装：$HOME/.workbuddy/binaries/python/envs/default/bin/pip install python-docx pymupdf beautifulsoup4 lxml" >&2
echo "或用 DOCMERGE_PYTHON 指定解释器" >&2
exit 1
