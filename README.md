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
기존 인스턴스 rename: `server.toml` name 을 새 이름으로 수정 → `setup/rename.sh <old>`(구 유닛 제거) → `setup/install.sh` → `enable --now <new>`.

## Windows fallback

`git checkout windows-stable` — 원 Windows 버전 전체. (`Tally 시작.bat` 은 과도기 편의를 위해 `main` 에도 유지되므로, 그 파일만 필요하면 브랜치 전환 불필요.)
