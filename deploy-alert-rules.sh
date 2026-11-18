#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for python in python3 python; do
  if command -v "$python" >/dev/null 2>&1 && "$python" -c 'import sys; sys.exit(sys.version_info.major != 3)' >/dev/null 2>&1; then
    script="$script_dir/scripts/metric_alerts.py"
    if command -v cygpath >/dev/null 2>&1; then script=$(cygpath -w "$script"); fi
    MSYS_NO_PATHCONV=1 "$python" "$script" "$@"
    exit $?
  fi
done
printf 'Error: Python 3 is required\n' >&2
exit 1
