# setup/sps_authorize.py — 브라우저(Chrome) 있는 머신에서 1회 실행해 sps_token.json 발급.
# SBS Labs 통합계정(sps.sbs.co.kr) 로그인 + 문자 인증코드가 필요해 헤드리스 서버에서는
# 실행할 수 없다. 여기서 만든 sps_token.json(약 30일 유효)을 서버로 scp.
#   python setup/sps_authorize.py   (Google Chrome 설치 필요, repo 루트에 저장됨)
import getpass
import json
import os
import sys
import time
import base64
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "playwright가 필요합니다. `uv sync --group setup` (또는 `pip install playwright`) 후 다시 실행하세요.\n"
        "Google Chrome이 이미 설치되어 있으면 별도 브라우저 다운로드는 필요 없습니다."
    )

TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sps_token.json')
LOGIN_URL = 'https://sps.sbs.co.kr/schedule'


def decode_jwt_exp(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(data['exp'], tz=timezone.utc)
    except Exception:
        return None


def main():
    username = input("SBS Labs 아이디: ").strip()
    password = getpass.getpass("SBS Labs 비밀번호: ")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            os.path.join(os.path.dirname(TOKEN_FILE), '.sps_chrome_profile'),
            channel='chrome', headless=False,
            viewport={'width': 1200, 'height': 900},
        )
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until='networkidle', timeout=30000)
        time.sleep(1)

        body_text = page.inner_text('body')
        if '로그인' in body_text and '인증코드' not in body_text:
            page.locator("input[type='text'], input:not([type])").first.fill(username)
            page.locator("input[type='password']").first.fill(password)
            page.locator('button:visible').first.click()
            time.sleep(3)
            body_text = page.inner_text('body')

        if '인증코드' in body_text:
            code = input("휴대폰으로 발송된 인증코드를 입력하세요: ").strip()
            code_input = page.locator("input[type='text'], input[type='tel'], input[type='number']").last
            code_input.fill(code)
            page.locator('button:visible', has_text='확인').first.click()
            time.sleep(3)
            body_text = page.inner_text('body')

        if '로그인' in body_text[:20]:
            ctx.close()
            sys.exit("로그인 실패 — 아이디/비밀번호/인증코드를 확인하고 다시 실행하세요.")

        token = None
        for c in ctx.cookies():
            if c['name'] == 'labs-token' and 'sbs.co.kr' in c['domain']:
                token = c['value']
                break
        ctx.close()

    if not token:
        sys.exit("로그인은 됐지만 labs-token 쿠키를 못 찾았습니다 — SBS 쪽 로그인 방식이 바뀌었을 수 있습니다.")

    exp = decode_jwt_exp(token)
    with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'token': token,
            'obtained_at': datetime.now(timezone.utc).isoformat(),
            'expires_at': exp.isoformat() if exp else None,
        }, f, ensure_ascii=False, indent=2)

    print(f"sps_token.json 발급 완료 (repo 루트).")
    if exp:
        print(f"만료 예정: {exp.isoformat()} (그 전에 이 스크립트를 다시 실행하세요)")


if __name__ == '__main__':
    main()
