#!/usr/bin/env bash
# Compatibility wrapper — delegates to `vd-dev test golden`
# Core implementation is in visidata/dev_cli.py (cmd_test_golden).
VD_DEV_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec env PYTHONPATH="$VD_DEV_DIR:$PYTHONPATH" python3 -m visidata.dev_cli test golden "$@"
