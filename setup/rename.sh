#!/usr/bin/env bash
# setup/rename.sh <old-name> — 이름 변경 시 구 systemd 유닛 정리.
# 사용: server.toml 의 name 을 NEW 로 수정 후:
#   setup/rename.sh <old-name> && setup/install.sh && sudo systemctl enable --now <new-name>
set -euo pipefail
old="${1:?사용법: rename.sh <old-name>}"
sudo systemctl disable --now "${old}.service" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/${old}.service"
sudo rm -rf "/etc/systemd/system/${old}.service.d"
sudo systemctl daemon-reload
echo "구 유닛 '${old}' 제거. 다음: server.toml name 수정 → setup/install.sh → enable --now <new>."
