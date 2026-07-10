import socket
import threading
import time
import re
import os
import json
import tomllib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, Response, render_template, jsonify, request
from queue import Queue, Empty
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

app = Flask(__name__)

# ── 설정 ──────────────────────────────────────────────────────────────────────
DM7_HOST_DEFAULT = '192.168.3.210'
DM7_PORT  = 49280
THRESHOLD = -8000

KST = ZoneInfo('Asia/Seoul')

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
CALENDAR_IDS = {
    'TS-5':     '5de83aec4c228767f47eebf0fb64224907e55b2524727a126f85a09110b06cf1@group.calendar.google.com',
    'TS-5(방송)': 'ca3fa2c18049bab1922c832ebbb3903b387f3edfde6f4bdd0fa18637009a7903@group.calendar.google.com',
}
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), 'credentials.json')
TOKEN_FILE       = os.path.join(os.path.dirname(__file__), 'token.json')
SETTINGS_FILE    = os.path.join(os.path.dirname(__file__), 'settings.json')

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
    except (FileNotFoundError, tomllib.TOMLDecodeError, ValueError, IndexError, KeyError, AttributeError, TypeError):
        return defaults

def write_pidfile(path=None):
    path = path or os.path.join(os.path.dirname(__file__), 'data', 'tally.pid')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(str(os.getpid()))

def get_dm7_host():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f).get('dm7Host', DM7_HOST_DEFAULT)
    except Exception:
        return DM7_HOST_DEFAULT

# ── 상태 ──────────────────────────────────────────────────────────────────────
fader = {i: -32768 for i in range(8)}
on_air = False
clients: list[Queue] = []
clients_lock = threading.Lock()


# ── SSE 브로드캐스트 ───────────────────────────────────────────────────────────
def broadcast(state: str):
    with clients_lock:
        for q in clients:
            q.put(f"data: {state}\n\n")


# ── RCP 메시지 파싱 ────────────────────────────────────────────────────────────
_RE = re.compile(r'NOTIFY.*InCh/Fader/Level\s+(\d+)\s+\d+\s+(-?\d+)', re.ASCII)

def parse(line: str):
    m = _RE.search(line)
    if not m:
        return None, None
    ch, val = int(m.group(1)), int(m.group(2))
    return (ch, val) if 0 <= ch <= 7 else (None, None)


def calc_on_air() -> bool:
    return any(v > THRESHOLD for v in fader.values())


# ── Google Calendar 인증 ───────────────────────────────────────────────────────
_creds = None
_creds_lock = threading.Lock()

def get_calendar_service():
    """Calendar service 반환. 비대화형: token 로드/refresh만; 유효·갱신가능 토큰이 없으면
    RuntimeError. 서버는 절대 OAuth에서 블록되지 않음 (대화형 발급 = mint_token(), setup/authorize.py)."""
    global _creds
    with _creds_lock:
        if _creds is None or not _creds.valid:
            creds = None
            if os.path.exists(TOKEN_FILE):
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as f:
                        f.write(creds.to_json())
                else:
                    raise RuntimeError(
                        f"유효한 {TOKEN_FILE} 없음 — 브라우저 있는 머신에서 "
                        "`uv run python setup/authorize.py` 로 발급 후 서버에 배치하세요."
                    )
            _creds = creds
    return build('calendar', 'v3', credentials=_creds)


def mint_token():
    """대화형 OAuth(브라우저)로 token.json 생성. setup/authorize.py(브라우저 있는 머신) 전용
    — 서버(headless)에서는 호출하지 않음."""
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, 'w') as f:
        f.write(creds.to_json())
    return creds


# ── DM7 TCP 리스너 ─────────────────────────────────────────────────────────────
def dm7_listener():
    global on_air
    while True:
        try:
            host = get_dm7_host()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(10)
                sock.connect((host, DM7_PORT))
                sock.settimeout(30)
                print(f"[DM7] 연결 성공 → {host}:{DM7_PORT}", flush=True)

                buf = ""
                while True:
                    chunk = sock.recv(4096).decode('utf-8', errors='ignore')
                    if not chunk:
                        raise ConnectionResetError("연결 끊김")
                    buf += chunk

                    while '\n' in buf:
                        line, buf = buf.split('\n', 1)
                        line = line.strip()
                        if not line:
                            continue

                        ch, val = parse(line)
                        if ch is None:
                            continue

                        fader[ch] = val
                        db = val / 100
                        flag = "▲ ON AIR" if val > THRESHOLD else "▼ 대기"
                        print(f"[Fader] ch{ch+1:02d}  {db:+7.2f} dB  {flag}", flush=True)

                        new = calc_on_air()
                        if new != on_air:
                            on_air = new
                            label = 'ON_AIR' if on_air else 'STANDBY'
                            print(f"★ [Tally] → {label}", flush=True)
                            broadcast(label)

        except Exception as e:
            print(f"[DM7] 오류: {e}  → 5초 후 재연결", flush=True)
            time.sleep(5)


# ── Flask ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    return jsonify(status='ok')


@app.route('/settings', methods=['GET'])
def get_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({})

@app.route('/settings', methods=['POST'])
def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(request.get_json(force=True), f)
    except Exception:
        pass
    return ('', 204)


@app.route('/test/<state>')
def test_state(state):
    global on_air
    if state == 'on':
        on_air = True
        broadcast('ON_AIR')
    elif state == 'off':
        on_air = False
        broadcast('STANDBY')
    return ('', 204)


@app.route('/stream')
def stream():
    def gen():
        q: Queue = Queue()
        with clients_lock:
            clients.append(q)
        yield f"data: {'ON_AIR' if on_air else 'STANDBY'}\n\n"
        try:
            while True:
                try:
                    yield q.get(timeout=25)
                except Empty:
                    yield ": keepalive\n\n"
        finally:
            with clients_lock:
                if q in clients:
                    clients.remove(q)

    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/calendar')
def get_calendar():
    try:
        service = get_calendar_service()
        offset_sec = float(request.args.get('offset', 0))
        now = datetime.now(KST) + timedelta(seconds=offset_sec)
        now_str = now.strftime('%H:%M')
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        today_end   = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

        selected = request.args.getlist('cal')
        cal_ids = [CALENDAR_IDS[k] for k in selected if k in CALENDAR_IDS] or list(CALENDAR_IDS.values())

        all_events = []
        for cal_id in cal_ids:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=today_start,
                timeMax=today_end,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            for ev in result.get('items', []):
                start = ev['start'].get('dateTime', ev['start'].get('date', ''))
                end   = ev['end'].get('dateTime', ev['end'].get('date', ''))
                allday = 'dateTime' not in ev['start']

                if not allday:
                    start_dt = datetime.fromisoformat(start).astimezone(KST)
                    end_dt   = datetime.fromisoformat(end).astimezone(KST)
                    start_str = start_dt.strftime('%H:%M')
                    end_str   = end_dt.strftime('%H:%M')
                else:
                    start_str = '00:00'
                    end_str   = '23:59'

                all_events.append({
                    'summary': ev.get('summary', '(제목 없음)'),
                    'start': start_str,
                    'end': end_str,
                    'allday': allday,
                })

        all_events.sort(key=lambda x: x['start'])

        current = None
        prev_ev = None
        next_ev = None

        for i, ev in enumerate(all_events):
            if ev['start'] <= now_str <= ev['end']:
                current = ev
                prev_ev = all_events[i - 1] if i > 0 else None
                next_ev = all_events[i + 1] if i < len(all_events) - 1 else None
                break

        if current is None:
            past   = [ev for ev in all_events if ev['end'] < now_str]
            future = [ev for ev in all_events if ev['start'] > now_str]
            prev_ev = past[-1] if past else None
            next_ev = future[0] if future else None

        return jsonify({'prev': prev_ev, 'current': current, 'next': next_ev})

    except Exception as e:
        return jsonify({'prev': None, 'current': None, 'next': None, 'error': str(e)}), 500


if __name__ == '__main__':
    cfg = load_server_config()
    write_pidfile()
    if not os.path.exists(TOKEN_FILE):
        print(f"[Auth] 경고: {TOKEN_FILE} 없음 — /calendar 는 발급 전까지 에러 반환(핵심 tally 는 정상 동작).", flush=True)
    threading.Thread(target=dm7_listener, daemon=True, name='dm7').start()
    print(f"[tally] '{cfg['name']}' → http://{cfg['host']}:{cfg['port']}", flush=True)
    app.run(host=cfg['host'], port=cfg['port'], threaded=True)
