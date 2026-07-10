#!/usr/bin/env bash
# setup/firewall.sh — server.toml 의 port 를 ufw 로 open(멱등). ufw 없거나 inactive 면
# no-op(기본 서버는 inbound 이미 열림). 포트 기반 = program-agnostic.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
read_toml() { python3 -c "import tomllib,sys; print(tomllib.load(open(sys.argv[2],'rb'))['service'][0].get(sys.argv[1],''))" "$1" "$root/server.toml"; }
port="$(read_toml port)"
name="$(read_toml name)"
command -v ufw >/dev/null 2>&1 || { echo "ufw 미설치 — skip(inbound 이미 열림)."; exit 0; }
ufw status 2>/dev/null | grep -q "Status: active" || { echo "ufw inactive — skip."; exit 0; }
sudo ufw allow "${port}/tcp" comment "$name"
echo "방화벽 규칙 적용(ufw): ${port}/tcp"
