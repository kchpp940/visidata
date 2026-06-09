#!/usr/bin/env bash
# Wrapper for vd-dev test golden
# Usage: test.sh [-d] [-j N] [testname ...]
VD_DEV_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec env PYTHONPATH="$VD_DEV_DIR:$PYTHONPATH" python3 -m visidata.dev_cli test golden "$@"
