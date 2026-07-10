# setup/authorize.py — 브라우저 있는 머신(Mac)에서 1회 실행해 token.json 발급.
# headless 서버는 OAuth 브라우저 플로우를 못 하므로, 여기서 만든 token.json 을 서버로 scp.
#   uv run python setup/authorize.py   (repo 루트에 credentials.json 필요)
from tally_server import get_calendar_service

if __name__ == '__main__':
    get_calendar_service()
    print("token.json 발급 완료 (repo 루트).")
