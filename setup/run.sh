#!/usr/bin/env bash
# setup/run.sh — SSOT launch entrypoint. server-manager(server.toml)·systemd 유닛·
# 직접 실행 모두 이걸 호출. venv 인터프리터를 직접 써서 runtime uv/PATH 의존 제거
# (systemd 하에서 robust). -u = unbuffered(로그 즉시 스트리밍).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # -> repo 루트
cd "$root"
exec "$root/.venv/bin/python" -u tally_server.py
