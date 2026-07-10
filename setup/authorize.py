# setup/authorize.py — 브라우저 있는 머신(Mac)에서 1회 실행해 token.json 발급.
# headless 서버는 OAuth 브라우저 플로우를 못 하므로, 여기서 만든 token.json 을 서버로 scp.
#   uv run python setup/authorize.py   (repo 루트에 credentials.json 필요)
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tally_server import mint_token

if __name__ == '__main__':
    mint_token()
    print("token.json 발급 완료 (repo 루트).")
