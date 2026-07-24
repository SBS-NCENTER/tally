import socket
import sys
import threading
import time
import re
import os
import json
import tomllib
import urllib.parse

# 콘솔/서비스 로케일이 UTF-8이 아닐 때도 한글·특수문자 print()가 죽지 않도록 강제
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
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


def extract_calendar_id(raw: str) -> str:
    """구글 캘린더 공유 URL(...?src=xxx) 또는 원시 캘린더 ID 문자열에서 캘린더 ID만 추출."""
    raw = (raw or '').strip()
    if not raw:
        return ''
    if 'src=' in raw:
        try:
            query = urllib.parse.urlparse(raw).query
            qs = urllib.parse.parse_qs(query)
            if 'src' in qs and qs['src']:
                return qs['src'][0]
        except Exception:
            pass
    return raw

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
    with open(path, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))

def get_dm7_host():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            return json.load(f).get('dm7Host', DM7_HOST_DEFAULT)
    except Exception:
        return DM7_HOST_DEFAULT

# ── 상태 ──────────────────────────────────────────────────────────────────────
fader = {i: -32768 for i in range(8)}
on_air = False
dm7_state = False   # DM7 페이더 기준 on-air
gpio_state = False  # GPIO 접점 기준 on-air
clients: list[Queue] = []
clients_lock = threading.Lock()


# ── SSE 브로드캐스트 ───────────────────────────────────────────────────────────
def broadcast(state: str):
    with clients_lock:
        for q in clients:
            q.put(f"data: {state}\n\n")


def update_on_air():
    """DM7 / GPIO 등 여러 탈리 입력원 중 하나라도 on-air면 전체 on-air로 반영."""
    global on_air
    new = dm7_state or gpio_state
    if new != on_air:
        on_air = new
        label = 'ON_AIR' if on_air else 'STANDBY'
        print(f"★ [Tally] → {label}", flush=True)
        broadcast(label)


# ── RCP 메시지 파싱 ────────────────────────────────────────────────────────────
# DM7는 파라미터 변경을 자동으로 NOTIFY하지 않고 get 요청에만 응답하므로,
# "OK get ..." 응답과 (혹시 모를) "NOTIFY ..." 푸시를 모두 매치하도록 접두어 무관하게 파싱.
_RE = re.compile(r'InCh/Fader/Level\s+(\d+)\s+\d+\s+(-?\d+)', re.ASCII)

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
                    with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
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
    with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
        f.write(creds.to_json())
    return creds


# ── DM7 TCP 리스너 ─────────────────────────────────────────────────────────────
DM7_POLL_INTERVAL = 0.2  # get 요청 주기(초) — DM7는 파라미터 변경을 자동 push하지 않아 직접 폴링

dm7_sock_lock = threading.Lock()
dm7_current_sock = None
dm7_force_reconnect = threading.Event()

def dm7_request_reconnect():
    """설정에서 dm7Host가 바뀌면 호출 — 기존 연결을 즉시 끊어 재접속을 앞당김."""
    dm7_force_reconnect.set()
    with dm7_sock_lock:
        if dm7_current_sock is not None:
            try:
                dm7_current_sock.close()
            except Exception:
                pass

def dm7_listener():
    global dm7_state, dm7_current_sock
    while True:
        try:
            host = get_dm7_host()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(10)
                sock.connect((host, DM7_PORT))
                with dm7_sock_lock:
                    dm7_current_sock = sock
                print(f"[DM7] 연결 성공 → {host}:{DM7_PORT}", flush=True)

                buf = ""
                while True:
                    for ch in range(8):
                        sock.sendall(f"get MIXER:Current/InCh/Fader/Level {ch} 0\n".encode())

                    sock.settimeout(DM7_POLL_INTERVAL)
                    try:
                        while True:
                            chunk = sock.recv(4096)
                            if not chunk:
                                raise ConnectionResetError("연결 끊김")
                            buf += chunk.decode('utf-8', errors='ignore')
                    except socket.timeout:
                        pass

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
                    if new != dm7_state:
                        dm7_state = new
                        update_on_air()

        except Exception as e:
            with dm7_sock_lock:
                dm7_current_sock = None
            if dm7_force_reconnect.is_set():
                dm7_force_reconnect.clear()
                print(f"[DM7] IP 변경 감지 → 즉시 재연결", flush=True)
            else:
                print(f"[DM7] 오류: {e}  → 5초 후 재연결", flush=True)
                time.sleep(5)


# ── GPIO 접점 탈리 입력 (라즈베리파이 전용, DM7 같은 네트워크 탈리가 없는 콘솔용) ──────────
GPIO_PIN_DEFAULT = 17

def get_gpio_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            s = json.load(f)
        return (
            bool(s.get('gpioEnabled', False)),
            int(s.get('gpioPin', GPIO_PIN_DEFAULT)),
            bool(s.get('gpioActiveLow', True)),
        )
    except Exception:
        return False, GPIO_PIN_DEFAULT, True


def gpio_listener():
    """STUDER Vista8, SSL C100HD 등 네트워크 탈리가 없는 콘솔의 GPO/접점 출력을 라즈베리파이
    GPIO 핀으로 받아 tally 상태에 반영. RPi.GPIO가 없는 플랫폼(개발 PC 등)에서는 곧바로
    종료하며, 서버의 나머지 기능에는 전혀 영향을 주지 않음."""
    global gpio_state
    try:
        import RPi.GPIO as GPIO
    except Exception:
        print("[GPIO] RPi.GPIO 모듈 없음 → GPIO 탈리 입력 비활성화(라즈베리파이 전용 기능)", flush=True)
        return

    GPIO.setmode(GPIO.BCM)
    configured_pin = None
    try:
        while True:
            enabled, pin, active_low = get_gpio_settings()

            if not enabled:
                if configured_pin is not None:
                    GPIO.cleanup(configured_pin)
                    configured_pin = None
                if gpio_state:
                    gpio_state = False
                    update_on_air()
                time.sleep(1)
                continue

            if configured_pin != pin:
                if configured_pin is not None:
                    GPIO.cleanup(configured_pin)
                pull = GPIO.PUD_UP if active_low else GPIO.PUD_DOWN
                GPIO.setup(pin, GPIO.IN, pull_up_down=pull)
                configured_pin = pin
                print(f"[GPIO] 입력 시작 → BCM{pin} (active_{'low' if active_low else 'high'})", flush=True)

            raw = GPIO.input(configured_pin)
            active = (raw == GPIO.LOW) if active_low else (raw == GPIO.HIGH)
            if active != gpio_state:
                gpio_state = active
                update_on_air()

            time.sleep(0.1)
    finally:
        GPIO.cleanup()


# ── 생방송 모드 자동 종료 전환 ─────────────────────────────────────────────────
def auto_revert_watcher():
    """생방송(카운트다운) 모드 + 종료시각 + 자동전환(분) 설정이 있으면,
    종료시각 + N분이 지나면 일반(일정) 모드로 자동 전환."""
    while True:
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                s = json.load(f)
            revert_min = float(s.get('liveAutoRevertMinutes', 0) or 0)
            end_val = s.get('broadcastEndTime')
            if s.get('countdownMode') and revert_min > 0 and end_val:
                now = datetime.now(KST)
                end_time = datetime.strptime(end_val, '%H:%M:%S').time()
                end_dt = datetime.combine(now.date(), end_time, tzinfo=KST)
                if now >= end_dt + timedelta(minutes=revert_min):
                    s['countdownMode'] = False
                    s['broadcastTime'] = ''
                    s['broadcastEndTime'] = ''
                    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                        json.dump(s, f)
                    print(f"[AutoRevert] 생방송 종료 {revert_min}분 경과 → 일반 모드로 전환 (시작/종료 시각 초기화)", flush=True)
        except Exception:
            pass
        time.sleep(15)


# ── Flask ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/control')
def control():
    return render_template('control.html')


@app.route('/health')
def health():
    return jsonify(status='ok')


@app.route('/settings', methods=['GET'])
def get_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({})

@app.route('/settings', methods=['POST'])
def save_settings():
    try:
        new_settings = request.get_json(force=True)
        old_host = get_dm7_host()
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(new_settings, f)
        new_host = new_settings.get('dm7Host', DM7_HOST_DEFAULT)
        if new_host != old_host:
            dm7_request_reconnect()
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
        cal_ids = [CALENDAR_IDS[k] for k in selected if k in CALENDAR_IDS]
        custom_raw = request.args.get('customCal', '')
        custom_id = extract_calendar_id(custom_raw)
        if custom_id:
            cal_ids.append(custom_id)
        if not cal_ids:
            cal_ids = list(CALENDAR_IDS.values())

        all_events = []
        for cal_id in cal_ids:
            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=today_start,
                    timeMax=today_end,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
            except Exception:
                # 캘린더 하나가 잘못됐거나(오타) 접근 권한이 없어도 나머지 캘린더는 계속 표시
                continue

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
    threading.Thread(target=gpio_listener, daemon=True, name='gpio').start()
    threading.Thread(target=auto_revert_watcher, daemon=True, name='auto-revert').start()
    print(f"[tally] '{cfg['name']}' → http://{cfg['host']}:{cfg['port']}", flush=True)
    app.run(host=cfg['host'], port=cfg['port'], threaded=True)
