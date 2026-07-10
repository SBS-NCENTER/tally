# tally — Windows→Linux 이주 + ts5-server 배포 (design)

> 작성: 2026-07-09. 상태: **설계 승인됨(사용자)**, 구현 plan 대기.
> 원본 소스: `smb://10.10.70.154/뉴스센터/3_프로젝트/김훈 감독님께♥/tally` (제작자 = **Joo Ho Gun** / git author `hihogun25 <sbsnews1991@gmail.com>`).
> 타깃: **ts5-server** (HP EliteDesk 800 G3, Debian 13 trixie, 운영자 `ncenter`).

## 1. 목적 / 한 줄 요약

회사 뉴스센터 **tally(온에어 상황판)** 프로그램을 Windows 전용에서 **Linux 서버(ts5-server)로 이주**하고, 다른 웹서버 2개(`xr-freed-to-udp`, `schedule-dashboard`)와 동일하게 **systemd 시스템 유닛**으로 배포 + **server-manager에 등록**한다. 포트는 `schedule-dashboard`와 겹치는 5000 → **5005**로 변경하고, 추후 쉽게 바꿀 수 있도록 config-driven으로 만든다.

## 2. tally가 하는 일 (현재 동작)

Python **Flask** 앱 (`tally_server.py`, ~272줄). 세 가지 기능:

1. **온에어 감지** — Yamaha **DM7** 믹서에 TCP(RCP 프로토콜, `192.168.3.210:49280`)로 붙어, InCh Fader Level을 파싱해 임계값(`-8000`) 초과 채널이 있으면 `ON_AIR`, 없으면 `STANDBY`. 백그라운드 daemon 스레드(`dm7_listener`)가 5초 재접속 루프.
2. **일정 표출** — Google **Calendar** readonly로 오늘 일정(TS-5 / TS-5(방송) 2개 캘린더)을 읽어 prev/current/next 타임라인 표출.
3. **실시간 방송** — 브라우저 클라이언트에 **SSE**(`/stream`)로 tally 상태 push. in-memory 공유 상태(`fader` dict, `on_air` bool, `clients` 리스트).

엔드포인트: `/`(표출 UI), `/stream`(SSE), `/calendar`, `/settings`(GET/POST), `/test/<on|off>`(DM7 없이 상태 테스트).

**아키텍처 제약(중요):** in-memory 공유 상태 + SSE 장기 연결 + 백그라운드 리스너 스레드 → **반드시 단일 프로세스**. 멀티워커 WSGI면 상태가 워커별로 갈라져 broadcast가 일부 클라만 도달.

## 3. 승인된 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 포트 | **5005** (config-driven) | 5000은 `schedule-dashboard` 점유. `server.toml`에서 읽어 추후 쉽게 변경(server-manager edit-port 포함) |
| git 이주 | **히스토리 보존 + 시크릿 스크럽** | 현재 Windows 프로그램 상태 보존 필요. `filter-repo`로 시크릿만 제거(해시 재작성, 내용·저작자 유지) |
| WSGI | **Flask 내장 서버 유지** (`app.run threaded=True`) | SSE + 백그라운드 스레드 + 단일프로세스 제약에 가장 적합. waitress는 스레드 시작 위치 리팩터 + SSE 스레드 점유 이슈 |
| 이름 변경성 | **`server.toml` `name` = SSOT + install.sh name-driven** | 멀티 부조정실 시 서비스명 충돌 대비. 이름 변경 = 한 곳 수정 + install 재실행 (§A0) |
| 범위 | **코드 + repo + 실서버 배포까지** | tally가 ts5-server에서 실제 구동 + server-manager 등록 |

## 4. 설계

### A0. 인스턴스 이름 SSOT (멀티 부조정실 대비)

**모델:** GitHub repo(코드베이스) = **1개**. 부조정실별로 그 repo를 clone해 **인스턴스(서비스) 이름만 다르게** 배포한다(예: `tally`, `tally-ts6`). repo를 부조정실마다 따로 만들지 않는다(코드 fork = 유지보수 지옥). → **충돌하는 것은 GitHub repo 이름이 아니라 배포된 systemd 서비스 이름**이고, 그걸 바꾸기 쉽게 만드는 게 이 설계의 목표.

**SSOT = `server.toml`의 `name`.** 이름이 파생되는 모든 곳을 여기서 유도한다:

```text
┌──────────────────────────────┬───────────────────────────────────────────┐
│ 파생 대상                    │ 유도 방식                                  │
├──────────────────────────────┼───────────────────────────────────────────┤
│ server.toml autostart_service│ = name (같은 파일, 같은 값 — 주석으로 명시)│
│ systemd unit 파일명          │ install.sh가 `${name}.service`로 설치       │
│ unit Description             │ install.sh가 name으로 치환(템플릿)         │
│ WorkingDirectory / 배포 경로 │ install.sh가 `$(pwd)` 사용(하드코딩 안 함) │
│ server-manager 등록          │ `/opt/<name>` 경로로 등록(경로가 name 따름)│
└──────────────────────────────┴───────────────────────────────────────────┘
```

- **unit = 정적 파일명 아님, 템플릿.** `setup/tally.service`는 템플릿(고정 `Description`/이름 placeholder)이고, `install.sh`가 `server.toml`에서 `name`을 읽어 `/etc/systemd/system/${name}.service`로 설치 + Description 치환. (siblings는 정적 `<name>.service`지만, tally는 rename 요구사항 때문에 의도적 divergence.)
- **`install.sh`는 name-driven:** ① `server.toml`에서 `name` 파싱 → ② `${name}.service`로 unit 생성 → ③ drop-in `User=ncenter`/`Group=ncenter`/`WorkingDirectory=$(pwd)`(하드코딩 경로 제거) → ④ `daemon-reload` + `enable --now ${name}`.
- **새 부조정실 배포 = 코드 수정 0.** 같은 repo clone → 그 배포본의 `server.toml`에서 `name`·`port`만 수정(배포 클론은 push 대상이 아니라 로컬 수정 OK) → `install.sh`. (부조정실별 캘린더/DM7는 §7 확장 지점 참조.)
- **기존 인스턴스 rename:** `setup/rename.sh <old>`(구 unit stop/disable/rm) 후 `server.toml` name 수정 → `install.sh` 재실행. (rename.sh는 얇은 헬퍼, 선택.)
- **코드(tally_server.py)는 이름 비의존:** 로직에 인스턴스 이름 불필요. pidfile은 `data/tally.pid` 고정(배포 디렉토리별 격리라 인스턴스 간 충돌 없음).

### A. Repo & git 전략 — `SBS-NCENTER/tally` (private)

- 기존 7커밋 히스토리(브랜치 `master`/`v1-stable`/`v1.1`/`v1.2`/`v1.3-portrait-mode`)를 **보존**하되, `git filter-repo`로 **`credentials.json` / `token.json` / `tally.log`를 전 이력에서 제거**. 이 파일들은 첫 커밋 `f41975d`에 들어갔다 `28ea68e`에서 빠졌으나 과거 커밋에 잔존 → push 전 반드시 스크럽.
- 마지막 순수 Windows 커밋(`44c7cb8`, v1.3-portrait-mode HEAD)에 **`windows-stable` 태그** = fallback 안전망 (다른 세 repo와 동일 규약).
- `main` = 그 위에 de-Windows-ify 커밋. 원작자 커밋 author(`hihogun25`)는 유지, **이주 커밋만 SBS-NCENTER noreply**(repo-local `user.email`).
- 잘못 추적된 `__pycache__/tally_server.cpython-314.pyc` 제거(이미 filter-repo에서 `__pycache__/` 제외 처리하면 함께 정리).
- `.gitignore`는 이미 `credentials.json`/`token.json`/`tally.log`/`__pycache__/`/`*.pyc` 제외 → 유지.

### B. de-Windows-ify (코드 변경)

- **포트/호스트 config-driven:** `tally_server.py`의 `app.run(host='0.0.0.0', port=5000)` → `server.toml`에서 `host`/`port`를 읽어 사용(기본 5005). server-manager가 `server.toml`의 `port`를 편집하면 재시작 시 반영.
- **WSGI:** Flask 내장 서버 유지. 단, `__main__` 블록의 host/port만 config에서 주입. 백그라운드 스레드 시작·OAuth 사전확인 로직은 그대로 `__main__`에 둠(내장 서버라 유효).
- **Windows fallback 보존:** `Tally 시작.bat`은 삭제하지 않음(Windows에서 여전히 동작). Linux 실행 경로는 `setup/`로 추가.
- **의존성:** `pyproject.toml`(uv 관리) — `flask`, `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`, `google-api-python-client`. (다른 두 repo와 동일하게 uv.)
- **OAuth:** 코드 로직은 그대로(`token.json` 있으면 refresh, 없으면 `run_local_server`). 서버는 headless라 **미리 만든 `token.json`을 secret으로 배치** → refresh_token 자동 갱신만 동작. `run_local_server` 경로는 서버에서 안 타야 함(= 유효한 token.json 필수).
  - **참고(사용자):** 이 캘린더는 **`schedule-dashboard`와 동일한 Google 계정**. → scope(`calendar.readonly`)가 일치하면 `/opt/schedule-dashboard`의 `token.json` **재활용 가능**(Mac 재발급 불필요). 불일치 시 Mac에서 tally scope로 1회 발급.
- **`/health` 엔드포인트 추가:** siblings의 `server.toml`이 `health_path = "/health"`를 선언하고 server-manager가 이를 헬스체크에 사용 → tally에도 `@app.route('/health')` → `jsonify(status='ok')` **추가**(사소, 등록·모니터 호환).
- **pidfile 기록:** siblings와 동일하게 startup 시 `data/tally.pid` 기록(server.toml `pidfile` 선언과 일치). 몇 줄.

### C. setup/ (배포 산출물 — 다른 두 repo와 동일 패턴)

- **`server.toml`** — siblings 스키마와 동일 필드: `name = "tally"`(**SSOT**, §A0), `type = "web"`, `description`, `command = "bash setup/run.sh"`, `host = "0.0.0.0"`, `port = 5005`, `health_path = "/health"`, `pidfile = "data/tally.pid"`, `autostart_service = "tally"`(주석으로 `= name` 명시). server-manager descriptor.
- **`setup/install.sh`** — **name-driven**(§A0): `server.toml`에서 `name` 파싱 → `uv sync` → `${name}.service` unit 설치(템플릿 Description 치환) → drop-in(`User/Group=ncenter`, `WorkingDirectory=$(pwd)`) → `daemon-reload` + `enable --now ${name}`.
- **`setup/run.sh`** — 진입점(`uv run python tally_server.py`), cwd = repo 루트(config·templates·secrets 상대경로 해석).
- **`setup/tally.service`** — systemd unit **템플릿**(install.sh가 name으로 파일명·Description 유도). ⚠️ **`WorkingDirectory`에 인라인 `#` 주석 절대 금지**(systemd가 줄 전체를 값으로 읽어 `200/CHDIR` — 다른 두 repo에서 겪은 버그). 주석은 윗줄로.
- **`setup/rename.sh`** (선택, 얇은 헬퍼) — `<old>` unit stop/disable/rm → 이후 `server.toml` name 수정 + `install.sh` 재실행으로 신규명 적용.
- **`setup/firewall.sh`** — `port`(기본 5005)/tcp open. 가능하면 server.toml에서 port 읽어 하드코딩 회피.

### D. 실서버 배포 (ts5-server)

1. `/opt/tally`(ncenter 소유)에 `git clone`.
2. `setup/install.sh`(uv sync).
3. **secrets 배치:** `credentials.json` + `token.json`. **scope 일치 시 `/opt/schedule-dashboard`의 token 재활용**, 아니면 Mac에서 OAuth 1회 실행해 생성 → `scp`. (readonly calendar scope, 동일 계정.)
4. `install.sh` 실행 → `server.toml`의 `name`으로 `${name}.service` 설치 + drop-in(`User=ncenter`, `WorkingDirectory=$(pwd)`) — root 실행·경로 하드코딩 회피.
5. `firewall.sh`로 port(5005) open.
6. `systemctl enable --now ${name}` → `active(running)` 확인.
7. **검증:** `curl http://ts5-server:5005/health` → `{status:ok}`, 브라우저 `/` 표출 + `/test/on`·`/test/off`로 ON_AIR/STANDBY 토글(DM7 없이). Calendar 표출 확인(token 유효 시).
8. server-manager `config/registered.json`(= `/opt/server-manager/config/`)에 `/opt/tally` 추가 → 목록 등재. (실제 TUI start/stop 제어는 server-manager **Phase 2** 후.)

### E. 병행 / 후속 (배포와 별개, 이 이주의 blocker 아님)

- 🟡 **DM7 망 라우팅** — ts5-server(`10.10.204.x`)에서 DM7(`192.168.3.210`) 현재 도달 불가(ping/TCP 실패). route는 default gw(`10.10.204.1`) 경유로 잡히나 응답 없음. **네트워크/방화벽 확인 필요** — 풀려야 실제 on-air 감지 동작. DM7 host는 `settings.json`의 `dm7Host`로 변경 가능(코드 이미 지원).
- 🟡 **OAuth token 만료** — Google OAuth 앱이 "testing" publishing 상태면 refresh_token이 7일 후 만료. 배치 전 앱 상태 확인(만료 시 주기적 재발급 또는 "production" 전환 필요).

## 5. 검증 기준

- 로컬(Mac/dev): `uv sync` OK, `uv run python tally_server.py`로 기동 → `/` 200, `/test/on` → SSE `ON_AIR` 수신, `/test/off` → `STANDBY`.
- 실서버: `systemctl status tally` = active(running), `http://ts5-server:5005/` 표출, `/test/*` 토글 동작.
- git: `credentials.json`/`token.json`/`tally.log`가 **전 이력에서** 사라짐(`git log --all --full-history -- <파일>` 빈 결과), `windows-stable` 태그 존재, main HEAD = de-Windows-ify.

## 6. docs & 레지스트리 갱신

- `docs/tally/STATUS.md`(cold-start 진입점) + `WORKLOG.md` 생성.
- `INDEX.md`에 `tally` 프로젝트 등록(코드 경로·repo URL·last-pushed).
- spec/plan dual-home: canonical = repo `docs/superpowers/{specs,plans}`(repo 생성 시 이 파일 복사) + mirror = 여기(vault).

## 7. Out of scope (YAGNI) + 확장 지점

- server-manager Phase 2(시스템 유닛 TUI 제어) 구현 — 별도 트랙. tally는 등록만.
- DM7 망 라우팅 해결 — 네트워크 작업(위 E).
- waitress/gunicorn 전환 — 현 구조에 불필요.
- UI 리디자인 — 표출 UI(`index.html`)는 그대로.
- **부조정실별 인스턴스 config(캘린더/DM7/threshold) — 이번엔 안 함(확장 지점만 문서화).** 이번 이주는 **이름 SSOT까지만**(사용자 명시 focus + 캘린더는 "참고만"). 실제 2번째 부조정실이 생길 때: 현재 코드 상수(`CALENDAR_IDS`, `THRESHOLD`, `DM7_HOST_DEFAULT`)를 앱이 **읽기만** 하는 `config.json`(git-ignored, 앱이 절대 안 씀)으로 추출 → 새 부조정실 = `config.json` + `server.toml name/port` 수정만. (`settings.json`은 UI POST가 통째로 덮어써서 부적합.) name-driven 구조가 이미 이 확장의 절반(배포/서비스명)을 커버하므로, 나머지 절반(모니터 대상 config)은 그때 소량 작업.

## 8. 미해결 항목 (구현 plan에서 확정)

- `filter-repo` 대상에 오래된 v1.x 브랜치를 push할지(기본: `main` + `windows-stable` 태그만 push, 나머지 브랜치는 로컬 보존/미push) — plan에서 결정.
- `run.sh` 진입점 형태(직접 `python tally_server.py` vs 얇은 wrapper) — plan에서 확정.
- `install.sh`의 `server.toml` `name` 파싱 방식(순수 bash grep/sed vs `python -c`로 tomllib) — plan에서 확정.
- `token.json` 재활용 여부 = schedule-dashboard scope 실측 후 확정(일치 시 재활용, 아니면 재발급).

## 9. 구현 중 변경 (2026-07-10, 최종 whole-branch 리뷰 반영)

- **캘린더 인증을 서버 startup에서 디커플 (option C).** 최종 리뷰 발견: `__main__`이 `app.run()` 전 `get_calendar_service()`를 eager 호출 → token 없거나 만료 시 headless에서 `run_local_server`가 **hang** → tally 전체(on-air 감지 포함)가 silent down. 방송용 핵심 기능이 Google/토큰에 결합되는 위험.
  - 수정: `get_calendar_service()` = **비대화형**(token 로드/refresh만, 없으면 `RuntimeError`) → `/calendar`의 기존 try/except가 잡아 에러 JSON 반환. 대화형 OAuth는 `mint_token()`으로 분리(`setup/authorize.py` 전용, 서버 경로엔 없음). `__main__`은 eager 인증 제거(token 없으면 **비치명 경고만**) → 서버+DM7+SSE+`/health`는 토큰 상태와 무관하게 항상 기동.
  - → §B의 "OAuth ... 없으면 `run_local_server`"는 **서버 경로에선 폐기**(raise). `run_local_server`는 `mint_token`(발급 도구)에만.
- **`setup/authorize.py`**: `uv run` 실행 시 repo root를 `sys.path`에 추가(`package=false`라 `ModuleNotFoundError` 방지) + `mint_token()` 호출.
- **`load_server_config`**: 구조적 `server.toml` 오류(`[service]` 오타 등) 시에도 기본값 fallback(except에 `KeyError`/`AttributeError`/`TypeError` 추가) — 멀티 부조정실 hand-edit 대비(T4b).
