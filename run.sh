#!/usr/bin/env bash
# 启动贾维斯语音助手
cd "$(dirname "$0")"
exec ./.venv/bin/python -u -m jarvis "$@"
