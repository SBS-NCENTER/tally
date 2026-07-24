import socket
import sys
import threading
import time
import re
import os
import json
import tomllib
import urllib.parse
import urllib.request
import hashlib
import secrets

# 콘솔/서비스 로케일이 UTF-8이 아닐 때도 한글·특수문자 print()가 죽지 않도록 강제
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, Response, render_template, jsonify, request, session, redirect, url_for
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
CONTROL_AUTH_FILE = os.path.join(os.path.dirname(__file__), 'control_auth.json')
FLASK_SECRET_FILE = os.path.join(os.path.dirname(__file__), 'flask_secret.key')


def load_flask_secret():
    """세션 서명용 비밀키. 재시작해도 로그인이 풀리지 않도록 파일에 보관하고,
    없으면 새로 생성한다 — 커밋되면 안 되므로 .gitignore 처리."""
    try:
        with open(FLASK_SECRET_FILE, encoding='utf-8') as f:
            key = f.read().strip()
            if key:
                return key
    except Exception:
        pass
    key = secrets.token_hex(32)
    with open(FLASK_SECRET_FILE, 'w', encoding='utf-8') as f:
        f.write(key)
    return key


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode('utf-8')).hexdigest()


def get_control_pin_hash() -> str:
    try:
        with open(CONTROL_AUTH_FILE, encoding='utf-8') as f:
            return json.load(f).get('pin_hash', '')
    except Exception:
        return ''


app.secret_key = load_flask_secret()
app.permanent_session_lifetime = timedelta(days=365)  # 한 번 로그인하면 이 기기에서는 계속 유지


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


# ── SBS SPS 운행표 API (생방송 모드 시작/종료 시각 자동 감지) ───────────────────────
# sps.sbs.co.kr(SBS Labs 통합계정 SSO)에서 발급한 Bearer 토큰으로 운행표를 직접 조회.
# liveOrVcr=true인 항목(TS-n 부조종실 생방송 — 이 프로젝트가 있는 TS-5는 녹화 전용이라
# 항상 false) 중 현재 on-air인 항목을 찾아 생방송(카운트다운) 모드에 자동 반영한다.
# 토큰은 setup/sps_authorize.py(브라우저 있는 머신, 아이디/문자인증 1회)로 발급 —
# 약 30일짜리라 서버는 헤드리스로 이 토큰을 읽어 쓰기만 한다.
SPS_API_BASE = 'https://sps.sbs.co.kr:8123'
SPS_TOKEN_FILE = os.path.join(os.path.dirname(__file__), 'sps_token.json')
SPS_POLL_INTERVAL = 20  # 초 — 가벼운 JSON 호출이라 OCR보다 자주 확인 가능
SPS_DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')


def get_sps_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            s = json.load(f)
    except Exception:
        s = {}
    return {
        'enabled': s.get('spsAutoDetect', True),
        'studio': s.get('spsStudio', ''),
        'gap_minutes': float(s.get('spsGapMinutes', 0) or 0),
    }


def get_sps_token():
    try:
        with open(SPS_TOKEN_FILE, encoding='utf-8') as f:
            return json.load(f).get('token')
    except Exception:
        return None


def sps_api_get(path, token):
    req = urllib.request.Request(SPS_API_BASE + path, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))


def sps_broadcast_date(now):
    """운행표의 '하루'는 새벽 5시경 시작해 다음날 새벽까지 이어진다.
    자정~새벽 5시 사이면 아직 전날 운행표에 속하는 것으로 본다."""
    d = now.date()
    return d if now.hour >= 5 else d - timedelta(days=1)


def save_sps_schedule(date_str, repos):
    """그날 운행표 전체를 data/sps_schedule_<date>.json에 저장.
    폴링마다(20초) 매번 통째로 덮어써서 지연 등으로 시각이 밀리면 최신 값으로 자동 갱신된다.
    내용이 그대로면 디스크 쓰기를 건너뛴다(SD카드 쓰기 수명 고려)."""
    os.makedirs(SPS_DATA_DIR, exist_ok=True)
    path = os.path.join(SPS_DATA_DIR, f'sps_schedule_{date_str}.json')
    try:
        with open(path, encoding='utf-8') as f:
            if json.load(f).get('repos') == repos:
                return
    except Exception:
        pass
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'date': date_str, 'updated_at': datetime.now(KST).isoformat(), 'repos': repos}, f, ensure_ascii=False)


def sps_find_live_segment(now, studio='', gap_minutes=0):
    """생방송(liveOrVcr=true, studio 지정 시 그 부조정실만) 항목 중
    (1) 지금 진행 중인 것이 있으면 그것,
    (2) 없고 방금 끝난 항목의 종료+gap_minutes가 아직 안 지났으면 그 방금 끝난 항목,
    (3) 그것도 아니면 오늘 중 가장 가까운 다음 항목을
    찾아 (시작 'HH:MM:SS', 종료 'HH:MM:SS', 종료가 익일인지, 프로그램명) 반환.
    아무것도 없으면 None.
    수동입력과 같은 느낌으로 방송 시작 전부터 "몇분 후 시작"을 미리 보여주기 위해,
    진행 중인 것뿐 아니라 다음 예정 항목도 채워준다 — 시작하면 자연스럽게
    "경과" 표시로 넘어간다(카운트다운 위젯 자체가 이미 그렇게 동작함).
    gap_minutes는 방송이 끝난 후 다음 방송 정보로 즉시 넘어가지 않고 얼마나
    더 "방금 끝난 방송"을 보여줄지(0이면 즉시 전환) 설정.
    /daily-schedule/onairdate가 주는 onAirIndex는 실측 결과 최대 한 항목(수십초)
    지연될 수 있어 신뢰하지 않고, startTime+duration을 now와 직접 비교한다."""
    token = get_sps_token()
    if not token:
        raise RuntimeError(
            "sps_token.json 없음/만료 — 브라우저 있는 머신에서 "
            "`uv run python setup/sps_authorize.py` 로 재발급 후 서버에 배치하세요."
        )

    date_str = sps_broadcast_date(now).strftime('%Y-%m-%d')
    repos = sps_api_get(f'/daily-schedule/repos?date={date_str}&uhd=false&band=true', token)
    save_sps_schedule(date_str, repos.get('repos', []))

    candidates = []
    for entry in repos.get('repos', []):
        if not entry.get('liveOrVcr'):
            continue
        if studio and entry.get('videoSource') != studio:
            continue
        start_dt = datetime.strptime(entry['startTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
        end_dt = start_dt + timedelta(seconds=entry['duration'])
        candidates.append((start_dt, end_dt, entry.get('programName', '')))

    def to_result(c):
        start_dt, end_dt, program_name = c
        return start_dt.strftime('%H:%M:%S'), end_dt.strftime('%H:%M:%S'), end_dt.date() != start_dt.date(), program_name

    for c in candidates:
        if c[0] <= now < c[1]:
            return to_result(c)

    if gap_minutes > 0:
        ended = [c for c in candidates if c[1] <= now]
        if ended:
            last_ended = max(ended, key=lambda c: c[1])
            if now < last_ended[1] + timedelta(minutes=gap_minutes):
                return to_result(last_ended)

    upcoming = [c for c in candidates if c[0] > now]
    if upcoming:
        return to_result(min(upcoming, key=lambda c: c[0]))

    return None


def sps_listener():
    """SPS 운행표를 주기적으로 조회해서 생방송(liveOrVcr, 선택된 부조정실) 진행 항목을
    발견하면 생방송(카운트다운) 모드의 시작/종료 시각을 자동으로 채워준다. settings.json의
    spsAutoDetect를 false로 두면 즉시 끌 수 있다(스레드는 계속 돌되 아무것도 하지 않음).
    같은 방송 구간(key)에 대해서는 딱 한 번만 countdownMode를 강제로 켠다 — 그 뒤 사용자가
    컨트롤 페이지에서 수동으로 다른 모드로 바꾸면, 그 방송이 끝나고 다음 구간으로 넘어갈
    때까지는 존중해서 다시 켜지 않는다(매 폴링마다 강제로 되돌리면 수동 전환이 안 먹힘)."""
    last_applied = None
    last_error = None
    while True:
        try:
            cfg = get_sps_settings()
            if cfg['enabled']:
                found = sps_find_live_segment(datetime.now(KST), cfg['studio'], cfg['gap_minutes'])
                if found:
                    start_str, end_str, end_next_day, program_name = found
                    key = (start_str, end_str, program_name)
                    if key != last_applied:
                        with open(SETTINGS_FILE, encoding='utf-8') as f:
                            s = json.load(f)
                        s['countdownMode'] = True
                        s['broadcastTime'] = start_str
                        s['broadcastEndTime'] = end_str
                        s['broadcastTimeNextDay'] = False
                        s['broadcastEndTimeNextDay'] = end_next_day
                        s['broadcastProgramName'] = program_name
                        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                            json.dump(s, f)
                        print(f"[SPS] 생방송 감지 → {program_name} {start_str} ~ {end_str} 자동 반영", flush=True)
                        last_applied = key
            last_error = None
        except Exception as e:
            msg = str(e)
            if msg != last_error:
                print(f"[SPS] 오류: {msg}", flush=True)
                last_error = msg
        time.sleep(SPS_POLL_INTERVAL)


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
    if not session.get('control_authed'):
        return redirect(url_for('control_login'))
    return render_template('control.html')


@app.route('/control-login', methods=['GET', 'POST'])
def control_login():
    error = None
    if request.method == 'POST':
        expected = get_control_pin_hash()
        pin = request.form.get('pin', '')
        if expected and hash_pin(pin) == expected:
            session.permanent = True
            session['control_authed'] = True
            return redirect(url_for('control'))
        error = 'PIN이 올바르지 않습니다.'
    return render_template('control_login.html', error=error)


@app.route('/health')
def health():
    return jsonify(status='ok')


@app.route('/sps/live-schedule')
def sps_live_schedule():
    """오늘 운행표 캐시(data/sps_schedule_<date>.json)에서 생방송(liveOrVcr) 항목만
    (설정된 부조정실이 있으면 그 항목만) 골라 컨트롤 페이지에 보여줄 최소 정보로 반환.
    API를 다시 호출하지 않고 캐시만 읽는다."""
    if not session.get('control_authed'):
        return ('', 403)
    date_str = sps_broadcast_date(datetime.now(KST)).strftime('%Y-%m-%d')
    path = os.path.join(SPS_DATA_DIR, f'sps_schedule_{date_str}.json')
    try:
        with open(path, encoding='utf-8') as f:
            repos = json.load(f).get('repos', [])
    except Exception:
        repos = []

    studio = get_sps_settings()['studio']
    items = []
    for entry in repos:
        if not entry.get('liveOrVcr'):
            continue
        if studio and entry.get('videoSource') != studio:
            continue
        start_dt = datetime.strptime(entry['startTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
        end_dt = start_dt + timedelta(seconds=entry['duration'])
        items.append({
            'start': start_dt.strftime('%H:%M:%S'),
            'end': end_dt.strftime('%H:%M:%S'),
            'duration': entry['duration'],
            'videoSource': entry.get('videoSource', ''),
            'programName': entry.get('programName', ''),
        })
    return jsonify(items)


@app.route('/settings', methods=['GET'])
def get_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({})

@app.route('/settings', methods=['POST'])
def save_settings():
    if not session.get('control_authed'):
        return ('', 403)
    try:
        new_settings = request.get_json(force=True)
        old_host = get_dm7_host()
        # broadcastProgramName은 sps_listener만 채우는 서버 파생 필드 — 컨트롤 페이지는
        # 수동 입력 경로가 없으므로, 오래된/캐시된 페이지가 이 필드를 모른 채 저장해도
        # (또는 값이 비어 있어도) 덮어써지지 않도록 기존 값을 그대로 유지한다.
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                old_settings = json.load(f)
        except Exception:
            old_settings = {}
        new_settings['broadcastProgramName'] = old_settings.get('broadcastProgramName', '')
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
    threading.Thread(target=sps_listener, daemon=True, name='sps').start()
    print(f"[tally] '{cfg['name']}' → http://{cfg['host']}:{cfg['port']}", flush=True)
    app.run(host=cfg['host'], port=cfg['port'], threaded=True)
