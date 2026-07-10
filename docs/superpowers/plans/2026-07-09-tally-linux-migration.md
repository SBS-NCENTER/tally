# tally — Windows→Linux 이주 + ts5-server 배포 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 회사 뉴스센터 tally(온에어 상황판) Flask 앱을 Windows→Linux로 이주해 ts5-server에 systemd 시스템 유닛으로 배포하고 server-manager에 등록한다.

**Architecture:** 단일 파일 Flask 앱(`tally_server.py`) 유지 + Flask 내장 서버(SSE·백그라운드 스레드·in-memory 상태 때문에 단일 프로세스 필수). host/port/name을 `server.toml`(SSOT)에서 읽어 config-driven화하고, `install.sh`가 `name`으로 systemd 유닛·경로를 파생해 멀티 부조정실 rename을 쉽게 한다.

**Tech Stack:** Python ≥3.11, Flask, google-api-python-client(OAuth), uv(deps), systemd(system unit), pytest.

**입력 스펙:** `~/obsidian-vault/SBS/docs/tally/specs/2026-07-09-tally-linux-migration-design.md`

## Global Constraints

- **포트 = 5005** (config-driven; `server.toml`의 `port`에서 읽음). 하드코딩 금지.
- **이름 SSOT = `server.toml`의 `name`** (기본 `"tally"`). systemd 유닛명·Description·경로는 전부 여기서 파생.
- **단일 프로세스 필수** — 멀티워커 WSGI 금지(in-memory 공유 상태 + SSE + 백그라운드 리스너 스레드). Flask 내장 서버(`app.run(threaded=True)`) 유지.
- **systemd 값 줄에 인라인 `#` 주석 금지** (특히 `WorkingDirectory` → systemd가 줄 전체를 값으로 읽어 `200/CHDIR`). 주석은 윗줄로.
- **시크릿 절대 커밋 금지** — `credentials.json`/`token.json`/`tally.log`는 `.gitignore`로 제외 + 과거 이력에서 스크럽.
- **Python ≥3.11** (`tomllib` stdlib 사용).
- **git 명령은 사용자가 직접 실행**(학습 중). 서브에이전트는 코드 작성·테스트만, **commit 안 함** — 각 task 후 사용자가 commit. git/gh/ssh 명령은 인라인 `#` 주석과 함께 제시.
- **commit author** = 이주 커밋은 `SBS-NCENTER` noreply(repo-local `user.email`). 원작자 커밋 author(`hihogun25 <sbsnews1991@gmail.com>` = Joo Ho Gun)는 filter-repo가 보존.
- **타깃/운영자** = ts5-server(HP EliteDesk, Debian 13), `ncenter`. ssh alias: `ts5-server`(LAN) / `ts5-t`(tailscale).

---

## File Structure

```text
~/workspaces/SBS/tally/            (git-only, vault 밖)
├── tally_server.py                [modify]  config 읽기 + /health + pidfile
├── server.toml                    [create]  descriptor/SSOT (name/host/port/…)
├── pyproject.toml                 [create]  uv deps + pytest 설정
├── .gitignore                     [modify]  data/ .venv/ .DS_Store *.pid 추가
├── Tally 시작.bat                 [keep]    Windows fallback(수정 안 함)
├── templates/index.html           [keep]    표출 UI(수정 안 함)
├── static/image.png               [keep]
├── setup/
│   ├── run.sh                     [create]  실행 진입점(SSOT)
│   ├── install.sh                 [create]  name-driven 설치(uv sync + unit + drop-in)
│   ├── tally.service              [create]  systemd unit 템플릿
│   ├── firewall.sh               [create]  port(server.toml) ufw open
│   ├── rename.sh                  [create]  구 unit 정리 헬퍼
│   └── authorize.py              [create]  Mac에서 token.json 발급용
├── tests/
│   ├── test_parse.py              [create]  parse()/calc_on_air() characterization
│   ├── test_config.py             [create]  load_server_config()
│   └── test_routes.py             [create]  /health
├── README.md                      [create]  이주 상태 + 실행 + rename how-to
└── docs/superpowers/{specs,plans}/[create]  spec/plan canonical 복사
```

---

## Task 1: 워크스페이스로 가져오기 + git 준비 (사용자 git 실행)

**Files:**
- Copy: `/Volumes/뉴스센터/3_프로젝트/김훈 감독님께♥/tally` → `~/workspaces/SBS/tally`

**Deliverable:** `.git` 포함 로컬 사본 + `git-filter-repo` 설치 + 히스토리 파악.

- [ ] **Step 1: SMB에서 워크스페이스로 복사** (`.git` 포함)

```bash
cp -R "/Volumes/뉴스센터/3_프로젝트/김훈 감독님께♥/tally" ~/workspaces/SBS/tally  # -R = 하위 전체 재귀 복사(.git 포함)
cd ~/workspaces/SBS/tally                                                          # 이후 모든 명령은 여기서
```

- [ ] **Step 2: macOS 파일명 정규화 이슈 방지** (NFD/NFC — `Tally 시작.bat`)

```bash
git config core.precomposeunicode true   # macOS가 주는 NFD 경로를 git이 NFC로 정규화(한글 파일명 phantom 방지)
git status -sb                            # '?? Tally 시작.bat' 같은 phantom이 없어야 정상
git checkout -- "Tally 시작.bat" 2>/dev/null || true   # 그래도 phantom이면 추적본(NFC)으로 복원
```

기대: `git status`가 깨끗(untracked = `.DS_Store` 정도만). 만약 `Tally 시작.bat`이 계속 modified/untracked로 꼬이면 → 이 git 작업(Task 1–2)을 dev 박스(chunbay-x86, Linux)에서 수행(경로 NFC 보존).

- [ ] **Step 3: git-filter-repo 설치**

```bash
brew install git-filter-repo   # 히스토리 재작성 도구(시크릿 스크럽에 사용). 대안: pipx install git-filter-repo
git filter-repo --version      # 설치 확인(버전 출력되면 OK)
```

- [ ] **Step 4: 현재 히스토리·브랜치 파악** (스크럽 전 스냅샷)

```bash
git log --all --oneline --graph --decorate   # 7커밋 + 브랜치(master/v1-stable/v1.1/v1.2/v1.3-portrait-mode) 구조 확인
git branch -a                                 # 현재 체크아웃 = v1.3-portrait-mode 여야 함(= 최신 Windows 상태)
git log --all --full-history --oneline -- credentials.json token.json   # 시크릿이 f41975d/28ea68e에 걸림을 재확인
```

기대: 마지막 커밋 = `44c7cb8`(세로 모드 추가…), 시크릿이 과거 커밋에 존재.

**(commit 없음 — 준비 단계)**

---

## Task 2: 시크릿 스크럽 + main/windows-stable 정리 (사용자 git 실행)

**Files:** 히스토리 재작성(전 커밋), 브랜치/태그 재구성.

**Deliverable:** `credentials.json`/`token.json`/`tally.log`/`__pycache__`가 전 이력에서 제거된 repo + `main` 브랜치 + `windows-stable` 태그.

- [ ] **Step 1: filter-repo로 시크릿·잡파일 전 이력 제거**

(⚠️ `\` 줄바꿈 뒤 인라인 주석은 명령을 깨뜨리므로, 실행은 아래 한 줄을 복사하고 각 플래그 설명은 그 아래 목록 참조)

```bash
git filter-repo --invert-paths --path credentials.json --path token.json --path tally.log --path-glob '__pycache__/*' --force
```

플래그 설명:
- `--invert-paths` — 지정 경로를 **제거**(기본은 '보존'이라 반전 필수)
- `--path credentials.json` — OAuth client secret 제거
- `--path token.json` — OAuth 사용자 토큰 제거
- `--path tally.log` — 런타임 로그(44KB) 제거
- `--path-glob '__pycache__/*'` — 커밋된 `.pyc` 제거
- `--force` — fresh clone 아니어도 강행(SMB 사본이라 안전)

- [ ] **Step 2: 시크릿이 전 이력에서 사라졌는지 검증**

```bash
git log --all --full-history --oneline -- credentials.json token.json tally.log   # ← 반드시 '빈 출력'
git log --all --oneline --graph --decorate                                        # 커밋 내용·저작자(hihogun25) 유지 확인
```

기대: 첫 명령 **빈 출력**(시크릿 흔적 0). 실패 시 push 금지.

- [ ] **Step 3: main 브랜치 확정 + windows-stable 태그**

```bash
git branch -m main                              # 현재 브랜치(v1.3-portrait-mode=최신 Windows 상태)를 main으로 rename
git tag windows-stable                          # 현재 HEAD(순수 Windows, 스크럽됨)에 fallback 태그
git tag -l                                      # windows-stable 태그 확인
```

- [ ] **Step 4: 중복 브랜치 정리** (선택 — 히스토리가 선형이면 main에 포함됨)

```bash
git branch                                      # 남은 로컬 브랜치 목록
git branch -D v1-stable v1.1-ui-improvements v1.2-logo-redesign master 2>/dev/null || true  # main 계보에 포함된 중복 포인터 삭제(없으면 무시)
```

주의: `master`가 main과 **갈라져 있으면**(diverged) 삭제하지 말고 보고. 기본 가정 = 선형이라 안전 삭제.

- [ ] **Step 5: 이주 커밋용 author 고정** (원작자 커밋은 filter-repo가 이미 보존)

```bash
git config user.email "SBS-NCENTER@users.noreply.github.com"   # 이후 이주 커밋 author = 회사 계정
git config user.name  "SBS-NCENTER"
```

**(commit 없음 — 다음 코드 task부터 commit 시작)**

---

## Task 3: pyproject.toml + uv + pytest 하네스

**Files:**
- Create: `~/workspaces/SBS/tally/pyproject.toml`
- Create: `~/workspaces/SBS/tally/tests/test_parse.py`

**Interfaces:**
- Produces: uv 환경(`.venv`) + `uv run pytest` 실행 가능. 기존 순수 함수 `tally_server.parse(line) -> (ch|None, val|None)`, `tally_server.calc_on_air() -> bool` characterization 고정.

- [ ] **Step 1: pyproject.toml 생성**

```toml
[project]
name = "tally"
version = "1.3.0"
description = "뉴스센터 온에어 tally 상황판 (DM7 on-air 감지 + Google Calendar 표출 + SSE)"
readme = "README.md"
requires-python = ">=3.11"
authors = [{ name = "Joo Ho Gun" }]
dependencies = [
    "flask>=3.1.0",
    "google-auth>=2.0.0",
    "google-auth-oauthlib>=1.0.0",
    "google-auth-httplib2>=0.2.0",
    "google-api-python-client>=2.0.0",
]

[dependency-groups]
dev = ["pytest>=8.0.0"]

[tool.uv]
package = false

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

- [ ] **Step 2: 환경 생성**

Run: `cd ~/workspaces/SBS/tally && uv sync`
Expected: `.venv` 생성 + flask/google-* + pytest 설치. 에러 없이 완료.

- [ ] **Step 3: characterization 테스트 작성** (`tests/test_parse.py`)

```python
import tally_server


def test_parse_valid_line():
    line = "NOTIFY set MIXER:Current/InCh/Fader/Level 0 0 -1000"
    assert tally_server.parse(line) == (0, -1000)


def test_parse_channel_out_of_range():
    line = "NOTIFY set MIXER:Current/InCh/Fader/Level 9 0 -1000"
    assert tally_server.parse(line) == (None, None)


def test_parse_non_matching_line():
    assert tally_server.parse("garbage") == (None, None)


def test_calc_on_air_below_threshold_is_false():
    tally_server.fader = {i: -32768 for i in range(8)}
    assert tally_server.calc_on_air() is False


def test_calc_on_air_above_threshold_is_true():
    tally_server.fader = {i: -32768 for i in range(8)}
    tally_server.fader[3] = -5000  # THRESHOLD=-8000 보다 큼
    assert tally_server.calc_on_air() is True
```

- [ ] **Step 4: 테스트 실행 → 통과**

Run: `uv run pytest -v`
Expected: 5 passed. (기존 `parse`/`calc_on_air`가 이미 존재하므로 바로 통과 = 하네스 검증)

- [ ] **Step 5: (사용자 실행) commit**

```bash
git add pyproject.toml tests/test_parse.py   # uv.lock도 생성됐으면 함께: git add uv.lock
git commit -m "build: uv 프로젝트 설정 + parse/calc_on_air characterization 테스트"
```

---

## Task 4: config-driven host/port/name (server.toml 리더)

**Files:**
- Create: `~/workspaces/SBS/tally/server.toml`
- Modify: `~/workspaces/SBS/tally/tally_server.py` (config 리더 추가 + `__main__` 배선)
- Create: `~/workspaces/SBS/tally/tests/test_config.py`

**Interfaces:**
- Produces: `tally_server.load_server_config(path=SERVER_TOML) -> {"name": str, "host": str, "port": int}`. 파일 없거나 파싱 실패 시 기본값 `{"tally","0.0.0.0",5005}`.

- [ ] **Step 1: server.toml 생성** (sibling 스키마 동일)

```toml
[[service]]
name = "tally"
type = "web"
description = "뉴스센터 온에어 tally 상황판 (DM7 on-air + Google Calendar 표출)"
command = "bash setup/run.sh"
host = "0.0.0.0"
port = 5005
health_path = "/health"
pidfile = "data/tally.pid"
# autostart_service 는 반드시 위 name 과 동일하게 유지 (systemd 유닛명)
autostart_service = "tally"
```

- [ ] **Step 2: 실패 테스트 작성** (`tests/test_config.py`)

```python
import tally_server


def test_load_server_config_reads_values(tmp_path):
    toml = tmp_path / "server.toml"
    toml.write_text(
        '[[service]]\nname = "tally-ts6"\nhost = "127.0.0.1"\nport = 5006\n'
    )
    assert tally_server.load_server_config(str(toml)) == {
        "name": "tally-ts6",
        "host": "127.0.0.1",
        "port": 5006,
    }


def test_load_server_config_defaults_when_missing(tmp_path):
    assert tally_server.load_server_config(str(tmp_path / "nope.toml")) == {
        "name": "tally",
        "host": "0.0.0.0",
        "port": 5005,
    }
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: module 'tally_server' has no attribute 'load_server_config'`

- [ ] **Step 4: config 리더 구현** (`tally_server.py`)

`import json` 아래에 추가:

```python
import tomllib
```

`SETTINGS_FILE = ...` 줄 바로 아래(설정 섹션)에 추가:

```python
DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 5005
DEFAULT_NAME = 'tally'
SERVER_TOML = os.path.join(os.path.dirname(__file__), 'server.toml')

def load_server_config(path=SERVER_TOML):
    defaults = {'name': DEFAULT_NAME, 'host': DEFAULT_HOST, 'port': DEFAULT_PORT}
    try:
        with open(path, 'rb') as f:
            services = tomllib.load(f).get('service', [])
        svc = services[0] if services else {}
        return {
            'name': svc.get('name', defaults['name']),
            'host': svc.get('host', defaults['host']),
            'port': int(svc.get('port', defaults['port'])),
        }
    except (FileNotFoundError, tomllib.TOMLDecodeError, ValueError, IndexError):
        return defaults
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/test_config.py -v`
Expected: 2 passed.

- [ ] **Step 6: `__main__` 배선** (`tally_server.py` 맨 아래)

기존:
```python
if __name__ == '__main__':
    print("[Auth] Google 인증 확인 중...", flush=True)
    get_calendar_service()
    print("[Auth] 인증 완료!", flush=True)
    threading.Thread(target=dm7_listener, daemon=True, name='dm7').start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
```
로 바꿈:
```python
if __name__ == '__main__':
    cfg = load_server_config()
    print("[Auth] Google 인증 확인 중...", flush=True)
    get_calendar_service()
    print("[Auth] 인증 완료!", flush=True)
    threading.Thread(target=dm7_listener, daemon=True, name='dm7').start()
    print(f"[tally] '{cfg['name']}' → http://{cfg['host']}:{cfg['port']}", flush=True)
    app.run(host=cfg['host'], port=cfg['port'], threaded=True)
```

- [ ] **Step 7: 전체 테스트 재확인**

Run: `uv run pytest -v`
Expected: 7 passed (parse 5 + config 2).

- [ ] **Step 8: (사용자 실행) commit**

```bash
git add server.toml tally_server.py tests/test_config.py
git commit -m "feat: host/port/name을 server.toml에서 읽어 config-driven화 (port 5000→5005)"
```

---

## Task 5: /health 엔드포인트 + pidfile

**Files:**
- Modify: `~/workspaces/SBS/tally/tally_server.py` (`/health` route + `write_pidfile` + `__main__` 호출)
- Create: `~/workspaces/SBS/tally/tests/test_routes.py`

**Interfaces:**
- Produces: `GET /health -> 200 {"status":"ok"}`. `tally_server.write_pidfile(path=None)` — startup 시 `data/tally.pid` 기록.

- [ ] **Step 1: 실패 테스트 작성** (`tests/test_routes.py`)

```python
import tally_server


def test_health_returns_ok():
    client = tally_server.app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_write_pidfile_writes_pid(tmp_path):
    pid_path = tmp_path / "data" / "tally.pid"
    tally_server.write_pidfile(str(pid_path))
    assert pid_path.read_text().strip().isdigit()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_routes.py -v`
Expected: FAIL — `/health` 404 + `write_pidfile` AttributeError.

- [ ] **Step 3: `/health` route 추가** (`tally_server.py`의 `@app.route('/')` 아래)

```python
@app.route('/health')
def health():
    return jsonify(status='ok')
```

- [ ] **Step 4: `write_pidfile` 추가** (`load_server_config` 아래)

```python
def write_pidfile(path=None):
    path = path or os.path.join(os.path.dirname(__file__), 'data', 'tally.pid')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(str(os.getpid()))
```

- [ ] **Step 5: `__main__`에서 pidfile 기록** (`cfg = load_server_config()` 바로 아래에 추가)

```python
    write_pidfile()
```

- [ ] **Step 6: 테스트 통과 + 전체 확인**

Run: `uv run pytest -v`
Expected: 9 passed (parse 5 + config 2 + routes 2).

- [ ] **Step 7: (사용자 실행) commit**

```bash
git add tally_server.py tests/test_routes.py
git commit -m "feat: /health 엔드포인트 + pidfile 기록 (server-manager 호환)"
```

---

## Task 6: setup/ 배포 산출물 (name-driven)

**Files:**
- Create: `setup/run.sh`, `setup/install.sh`, `setup/tally.service`, `setup/firewall.sh`, `setup/rename.sh`, `setup/authorize.py`
- Modify: `.gitignore`

**Deliverable:** name-driven 배포 스크립트 일체 + `.gitignore` 갱신. 로컬에서 syntax/파싱 검증.

- [ ] **Step 1: `setup/run.sh`** (실행 진입점, sibling 패턴)

```bash
#!/usr/bin/env bash
# setup/run.sh — SSOT launch entrypoint. server-manager(server.toml)·systemd 유닛·
# 직접 실행 모두 이걸 호출. venv 인터프리터를 직접 써서 runtime uv/PATH 의존 제거
# (systemd 하에서 robust). -u = unbuffered(로그 즉시 스트리밍).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # -> repo 루트
cd "$root"
exec "$root/.venv/bin/python" -u tally_server.py
```

- [ ] **Step 2: `setup/tally.service`** (systemd 유닛 템플릿)

```ini
# setup/tally.service — systemd unit TEMPLATE.
# install.sh 가 server.toml 의 name 으로 /etc/systemd/system/<name>.service 로 설치하고
# __DESCRIPTION__ 을 치환하며, User/Group=ncenter + WorkingDirectory=<배포경로> drop-in 을 만든다.
# 값 줄에 인라인 '#' 주석 금지(systemd 가 줄 전체를 값으로 읽음 → 200/CHDIR).
[Unit]
Description=__DESCRIPTION__
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env bash setup/run.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: `setup/install.sh`** (name-driven 설치)

```bash
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
sudo sed -i "s|__DESCRIPTION__|${desc}|" "$unit"
dropin="/etc/systemd/system/${name}.service.d"
sudo mkdir -p "$dropin"
sudo tee "$dropin/override.conf" >/dev/null <<EOF
[Service]
User=ncenter
Group=ncenter
WorkingDirectory=${root}
EOF
sudo systemctl daemon-reload

"$here/firewall.sh" || true   # optional(ufw active 아니면 no-op)

echo "설치 완료. 기동: sudo systemctl enable --now ${name}"
```

- [ ] **Step 4: `setup/firewall.sh`** (port from server.toml)

```bash
#!/usr/bin/env bash
# setup/firewall.sh — server.toml 의 port 를 ufw 로 open(멱등). ufw 없거나 inactive 면
# no-op(기본 서버는 inbound 이미 열림). 포트 기반 = program-agnostic.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
port="$(python3 -c "import tomllib;print(tomllib.load(open('$root/server.toml','rb'))['service'][0]['port'])")"
command -v ufw >/dev/null 2>&1 || { echo "ufw 미설치 — skip(inbound 이미 열림)."; exit 0; }
ufw status 2>/dev/null | grep -q "Status: active" || { echo "ufw inactive — skip."; exit 0; }
sudo ufw allow "${port}/tcp" comment 'tally'
echo "방화벽 규칙 적용(ufw): ${port}/tcp"
```

- [ ] **Step 5: `setup/rename.sh`** (구 unit 정리 헬퍼)

```bash
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
```

- [ ] **Step 6: `setup/authorize.py`** (Mac에서 token.json 발급)

```python
# setup/authorize.py — 브라우저 있는 머신(Mac)에서 1회 실행해 token.json 발급.
# headless 서버는 OAuth 브라우저 플로우를 못 하므로, 여기서 만든 token.json 을 서버로 scp.
#   uv run python setup/authorize.py   (repo 루트에 credentials.json 필요)
from tally_server import get_calendar_service

if __name__ == '__main__':
    get_calendar_service()
    print("token.json 발급 완료 (repo 루트).")
```

- [ ] **Step 7: `.gitignore` 갱신** (기존 항목 아래에 추가)

기존:
```
credentials.json
token.json
tally.log
__pycache__/
*.pyc
```
아래에 추가:
```
.venv/
data/
*.pid
.DS_Store
.superpowers/
```

- [ ] **Step 8: 스크립트 syntax + name 파싱 검증**

```bash
chmod +x setup/*.sh                                   # 실행권한
for s in setup/*.sh; do bash -n "$s" && echo "OK: $s"; done   # bash 문법 검사(-n = 실행 안 하고 파싱만)
python3 -c "import tomllib;print(tomllib.load(open('server.toml','rb'))['service'][0]['name'])"   # → tally
```
Expected: 모든 스크립트 `OK`, 마지막 출력 `tally`.

- [ ] **Step 9: (사용자 실행) commit**

```bash
git add setup/ .gitignore
git commit -m "feat: name-driven setup/ (run/install/service/firewall/rename/authorize)"
```

---

## Task 7: README + docs + spec/plan canonical 복사

**Files:**
- Create: `README.md`
- Create: `docs/superpowers/specs/2026-07-09-tally-linux-migration-design.md` (spec 복사)
- Create: `docs/superpowers/plans/2026-07-09-tally-linux-migration.md` (이 plan 복사)
- Create(vault): `~/obsidian-vault/SBS/docs/tally/STATUS.md`, `WORKLOG.md`

**Deliverable:** README + repo 내 canonical spec/plan + vault STATUS/WORKLOG.

- [ ] **Step 1: README.md 작성**

```markdown
# tally — 온에어 상황판

뉴스센터 부조정실 온에어 tally. Yamaha **DM7** 믹서(RCP/TCP)로 on-air 감지 +
Google **Calendar** 일정 표출 + **SSE** 실시간 브로드캐스트. Python/Flask 단일 파일.

- 제작자: **Joo Ho Gun** (원 Windows 버전)
- 상태: **Linux 이주** — 단일 `main`(Linux primary), `windows-stable` 태그(Windows fallback).

## 실행 (Linux)

```bash
setup/install.sh                       # uv sync + systemd 유닛(name-driven) 설치
# credentials.json + token.json 을 repo 루트에 배치 (README §설정)
sudo systemctl enable --now tally      # 기동
curl localhost:5005/health             # {"status":"ok"}
```

직접 실행: `setup/run.sh` (내장 Flask 서버, 포트=server.toml).

## 설정

- **포트/이름**: `server.toml` (`port`, `name`). name = systemd 유닛명 SSOT.
- **DM7 IP**: 웹 UI 설정 → `settings.json` 의 `dm7Host` (기본 192.168.3.210).
- **시크릿**(gitignored): `credentials.json`(OAuth client) + `token.json`. 서버는 headless라
  브라우저 있는 머신에서 `uv run python setup/authorize.py` 로 `token.json` 발급 후 배치.

## 다른 부조정실에 설치 (이름 변경)

같은 repo 를 clone → 그 배포본의 `server.toml` 에서 `name`·`port` 수정 → `setup/install.sh`.
기존 인스턴스 rename: `setup/rename.sh <old>` → `server.toml` name 수정 → `install.sh` → `enable --now <new>`.

## Windows fallback

`git checkout windows-stable` — 원 Windows 버전(`Tally 시작.bat` 포함).
```

- [ ] **Step 2: repo 내 canonical spec/plan 복사**

```bash
mkdir -p docs/superpowers/specs docs/superpowers/plans
cp ~/obsidian-vault/SBS/docs/tally/specs/2026-07-09-tally-linux-migration-design.md docs/superpowers/specs/
cp ~/obsidian-vault/SBS/docs/tally/plans/2026-07-09-tally-linux-migration.md docs/superpowers/plans/
```

- [ ] **Step 3: vault STATUS.md + WORKLOG.md 작성** (`~/obsidian-vault/SBS/docs/tally/`)

`STATUS.md` — cold-start 진입점: 프로젝트 개요, 현재 상태(이주 완료/배포 단계), read-list(README·spec·machine doc), 검증 커맨드.
`WORKLOG.md` — `## 2026-07-09` 한 줄: repo 생성 + de-Windows-ify + setup/ + 배포.

- [ ] **Step 4: (사용자 실행) commit**

```bash
git add README.md docs/superpowers/
git commit -m "docs: README + canonical spec/plan (이주 설계·계획)"
```

(vault STATUS/WORKLOG 는 git 대상 아님 — syncthing 동기화)

---

## Task 8: GitHub repo 생성 + push (사용자 gh/git 실행)

**Deliverable:** `SBS-NCENTER/tally`(private) 생성 + `main` + `windows-stable` 태그 push + INDEX.md 갱신.

- [ ] **Step 1: gh 계정 = 회사로 전환**

```bash
gh auth switch --user SBS-NCENTER   # push 인증 주체를 회사 계정으로(개인 chunbay 아님)
gh auth status                      # active = SBS-NCENTER 확인
```

- [ ] **Step 2: private repo 생성**

```bash
gh repo create SBS-NCENTER/tally --private --description "뉴스센터 온에어 tally 상황판 (DM7 + Google Calendar + SSE)"  # 빈 원격 생성
```

- [ ] **Step 3: origin 연결 + push**

```bash
cd ~/workspaces/SBS/tally
git remote add origin https://github.com/SBS-NCENTER/tally.git   # 원격 등록
git push -u origin main                                          # main 브랜치 push(-u=upstream 설정)
git push origin windows-stable                                   # windows-stable 태그 push(fallback)
```

- [ ] **Step 4: 원격 검증**

```bash
gh repo view SBS-NCENTER/tally --json defaultBranchRef -q .defaultBranchRef.name   # → main
gh api repos/SBS-NCENTER/tally/git/refs/tags -q '.[].ref'                          # → refs/tags/windows-stable
```

- [ ] **Step 5: INDEX.md 갱신** (`~/obsidian-vault/SBS/INDEX.md`)

프로젝트 테이블에 `tally` 행 추가: 코드경로 `~/workspaces/SBS/tally`, repo URL, 가시성 private, last-pushed(hash/2026-07-09/기기). (git 대상 아님 — syncthing)

---

## Task 9: ts5-server 배포 + server-manager 등록

**Deliverable:** `/opt/tally` 시스템 유닛 `active(running)` + `/health ok` + server-manager `registered.json` 등재.

> ⚠️ 회사 라이브 서버 변경 — **enable/방화벽/시크릿 배치 전 사용자 확인**. ssh alias: `ts5-server`(LAN) 안 되면 `ts5-t`(tailscale).

- [ ] **Step 1: /opt/tally 로 clone** (ncenter 소유)

```bash
ssh ts5-t 'sudo git clone https://github.com/SBS-NCENTER/tally.git /opt/tally \
  && sudo chown -R ncenter:ncenter /opt/tally'   # ncenter 소유로(root 실행 회피)
```

- [ ] **Step 2: token.json 발급 (Mac) + 시크릿 배치**

```bash
# (Mac, repo 루트에 credentials.json 있어야 함) 브라우저로 OAuth 1회:
cd ~/workspaces/SBS/tally && uv run python setup/authorize.py   # token.json 생성
# 서버로 시크릿 복사:
scp credentials.json token.json ts5-t:/tmp/ \
  && ssh ts5-t 'mv /tmp/credentials.json /tmp/token.json /opt/tally/'
```

참고: scope(`calendar.readonly`)·OAuth client 가 schedule-dashboard 와 동일하면 `/opt/schedule-dashboard/token.json` 재활용 가능(위 발급 생략). 기본 = tally 자체 발급(안전).

- [ ] **Step 3: install.sh 실행** (uv sync + name-driven 유닛)

```bash
ssh ts5-t 'cd /opt/tally && setup/install.sh'   # .venv + tally.service + drop-in(User=ncenter, WD=/opt/tally)
```
Expected: "설치 완료. 기동: sudo systemctl enable --now tally"

- [ ] **Step 4: 기동** (사용자 확인 후)

```bash
ssh ts5-t 'sudo systemctl enable --now tally && sleep 2 && systemctl status tally --no-pager'
```
Expected: `active (running)`. 실패 시 `journalctl -u tally -n 50 --no-pager` 로 진단(특히 `200/CHDIR`=WorkingDirectory 주석, 시크릿 누락).

- [ ] **Step 5: 검증** (DM7 없이도)

```bash
ssh ts5-t 'curl -s localhost:5005/health; echo; curl -s -X GET localhost:5005/test/on -o /dev/null -w "%{http_code}\n"'
```
Expected: `{"status":"ok"}` + `204`. 브라우저 `http://<ts5-server-ip>:5005/` 표출 확인.

- [ ] **Step 6: server-manager 등록 (지연 — server-manager 미배포)**

⚠️ **현재 `/opt/server-manager` 없음**(server-manager 자체가 아직 ts5-server에 배포 안 됨 = server-manager STATUS의 Phase 3 미완). 따라서 지금 서버에 등록할 대상이 없다. 대신:
- **server-manager repo의 `config/registered.json`(planned Phase 3 config)에 `/opt/tally` 를 포함**하도록 기록한다. server-manager repo가 로컬에 clone돼 있으면(`~/workspaces/SBS/server-manager`) 그 목록을 `[/opt/xr-freed-to-udp, /opt/schedule-dashboard, /opt/tally]` 로 갱신(별도 commit/push는 server-manager 트랙에서).
- server-manager가 Phase 3로 배포될 때 tally가 자동 등재되고, Phase 2(시스템 유닛 제어) 후 TUI start/stop 가능.
- 이 지연 사실을 `docs/tally/STATUS.md` "## 배포 진행"에 명시.

- [ ] **Step 7: last-pushed·STATUS 갱신 + WORKLOG 로그**

배포 완료를 `docs/tally/STATUS.md`("## 배포 진행") + `WORKLOG.md` 에 기록. INDEX.md last-pushed 갱신.

---

## Self-Review (작성자 체크)

**Spec coverage:** §A0 이름 SSOT→T4(server.toml name)+T6(install.sh name-driven)+T6 rename.sh. §A git→T1/T2. §B de-Windows(port/health/pidfile/uv/OAuth)→T3/T4/T5. §C setup/→T6. §D 배포→T9. §6 docs/레지스트리→T7/T8. §E(DM7·OAuth 만료)=배포와 별개 flag(T9 Step2 note). ✅ 갭 없음.

**Placeholder scan:** 모든 코드 step에 실제 코드. server-manager `registered.json` 형식은 "실측 후 맞춤"(T9 S6) — 형식을 지금 모르므로 실행 시 확인이 정당(placeholder 아님). STATUS/WORKLOG 내용은 T7 S3에 무엇을 쓸지 명시.

**Type consistency:** `load_server_config` 리턴 키(name/host/port)가 server.toml·__main__·테스트에서 일치. `write_pidfile(path)` 시그니처 T5 정의=사용 일치. `parse`/`calc_on_air` 기존 시그니처 characterization 일치. ✅

**미해결(§8) 처리:** v1.x 브랜치=T2 S4 기본 삭제(diverged면 보고). run.sh 진입점=venv python 직접(T6 S1 확정). name 파싱=python3 tomllib(T6 S3 확정). token 재활용=T9 S2 조건부(기본 자체 발급). ✅
