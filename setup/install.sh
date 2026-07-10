#!/usr/bin/env bash
# setup/install.sh — 배포 박스에서 1회 실행.
# NAME-DRIVEN: server.toml(SSOT)의 name/description 을 읽어 systemd 유닛을
# <name>.service 로 설치하고, User/Group=ncenter + WorkingDirectory=<이 배포 디렉토리>
# drop-in 을 만든다. 다른 부조정실 배포: clone → server.toml(name/port) 수정 → 재실행.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
cd "$root"

command -v uv >/dev/null || { echo "ERROR: 'uv' 없음. 설치: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

read_toml() {  # $1 = key
  python3 -c "import tomllib,sys; print(tomllib.load(open('server.toml','rb'))['service'][0].get(sys.argv[1],''))" "$1"
}
name="$(read_toml name)"
desc="$(read_toml description)"
[ -n "$name" ] || { echo "ERROR: server.toml [[service]] 에 name 이 필요"; exit 1; }

uv sync   # .venv + deps

# 시크릿 경고(fail 안 함) — 앱 구동엔 필요하나 venv 는 만들어 둠
missing=(); for f in credentials.json token.json; do [ -f "$root/$f" ] || missing+=("$f"); done
[ "${#missing[@]}" -eq 0 ] || echo "WARNING: 시크릿 없음: ${missing[*]} — 구동 전 배치(README §설정)."

# systemd 유닛(system scope) — name-driven
unit="/etc/systemd/system/${name}.service"
sudo cp setup/tally.service "$unit"
sudo python3 - "$unit" "$desc" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
p.write_text(p.read_text().replace("__DESCRIPTION__", sys.argv[2]))
PY
dropin="/etc/systemd/system/${name}.service.d"
sudo mkdir -p "$dropin"
sudo tee "$dropin/override.conf" >/dev/null <<EOF
[Service]
User=ncenter
Group=ncenter
WorkingDirectory=${root}
EOF
sudo systemctl daemon-reload

bash "$here/firewall.sh" || true   # optional(ufw active 아니면 no-op)

echo "설치 완료. 기동: sudo systemctl enable --now ${name}"
