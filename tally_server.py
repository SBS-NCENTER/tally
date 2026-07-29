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
SPS_DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
SPS_LOOKAHEAD_DAYS = 3      # 오늘 남은 생방송이 없을 때 며칠 앞까지 다음 생방송을 찾아볼지
SPS_SLOW_INTERVAL = 20      # 초 — 평상시 폴링 간격
SPS_FAST_INTERVAL = 1       # 초 — 임박한 생방송 핸드오프 전후 폴링 간격
SPS_FAST_WINDOW = timedelta(minutes=5)      # 핸드오프 경계 앞뒤로 촘촘하게 볼 범위
SPS_HANDOFF_GAP = timedelta(minutes=10)     # 두 생방송 사이 이 안이면 "핸드오프"로 간주


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


SPS_HISTORY_FILE = os.path.join(SPS_DATA_DIR, 'broadcast_history.jsonl')


def sps_append_history(record):
    """추적이 끝난 생방송 하나의 예정/실제 시작·종료·제작시간을 data/broadcast_history.jsonl에
    한 줄씩 追加(append-only, SD카드 수명 고려해 통째로 재작성하지 않음). spsGapMinutes가
    0(즉시 전환)이면 화면이 다음 방송 대기로 바로 넘어가서 방금 끝난 방송 정보가 사라지므로,
    나중에 컨트롤 페이지에서 지난 방송들을 조회할 수 있게 남겨둔다."""
    os.makedirs(SPS_DATA_DIR, exist_ok=True)
    with open(SPS_HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def sps_read_history(date_str):
    """broadcast_history.jsonl에서 해당 날짜 기록을 eventId -> 레코드 딕셔너리로 읽어온다.
    같은 eventId가 여러 줄이면(이론상 없어야 하지만) 마지막 줄을 우선한다."""
    out = {}
    try:
        with open(SPS_HISTORY_FILE, encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get('date') == date_str and rec.get('eventId'):
                    out[rec['eventId']] = rec
    except Exception:
        pass
    return out


def sps_fetch_repos(date_str, token):
    """해당 날짜(방송일) 운행표 전체(repos 배열)를 조회하고 캐시 파일도 갱신해서 반환."""
    repos = sps_api_get(f'/daily-schedule/repos?date={date_str}&uhd=false&band=true', token).get('repos', [])
    save_sps_schedule(date_str, repos)
    return repos


def sps_parse_live(repos, studio=''):
    """레포 배열에서 생방송(liveOrVcr=true) 항목만 골라 딕셔너리 리스트로 반환.
    studio를 주면 그 부조정실만, 빈 문자열이면 스튜디오 무관 전체(핸드오프 판단용).
    eventId는 SPS가 부여하는 안 바뀌는 고유 ID — 종료시간이 사후에 갱신돼도 같은
    방송을 계속 같은 항목으로 추적하기 위한 식별자로 쓴다."""
    out = []
    for entry in repos:
        if not entry.get('liveOrVcr'):
            continue
        if studio and entry.get('videoSource') != studio:
            continue
        try:
            start_dt = datetime.strptime(entry['startTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
        except Exception:
            continue
        end_dt = start_dt + timedelta(seconds=entry['duration'])
        out.append({
            'start': start_dt,
            'end': end_dt,
            'programName': entry.get('programName', ''),
            'videoSource': entry.get('videoSource', ''),
            'eventId': entry.get('eventId'),
        })
    return out


def sps_pick_from_live(live, now, gap_minutes, on_air_event_id=None):
    """이미 골라둔(studio 필터링된) 오늘의 생방송 목록 안에서
    (0) onAirIndex가 실제로 이 목록의 한 항목을 가리키고 있으면 예정 시각과
    무관하게 그것을 최우선으로, (1) 없으면 예정 시각상 지금 진행 중인 것,
    (2) 없고 방금 끝난 항목의 종료+gap_minutes가 아직 안 지났으면 그것,
    (3) 없고 오늘 중 다음 항목이 있으면 그것을 골라 반환.
    셋 다 없으면 None(이 경우 호출자가 다른 날짜로 lookahead).
    (0)이 필요한 이유: 예정 종료시간은 방송이 실제로 늘어지는 바로 그 순간엔
    아직 옛날 값이라, 예정 시각만 보면 실제로는 아직 방송 중인데 이미 끝났다고
    착각해서 gap_minutes=0(즉시 전환)일 때 다음 방송 대기로 너무 일찍 넘어가
    버린다 — onAirIndex를 우선 신뢰하면 이 문제가 없다."""
    if on_air_event_id is not None:
        on_air_match = next((e for e in live if e['eventId'] == on_air_event_id), None)
        if on_air_match is not None:
            return on_air_match
    for e in live:
        if e['start'] <= now < e['end']:
            return e
    if gap_minutes > 0:
        ended = [e for e in live if e['end'] <= now]
        if ended:
            last_ended = max(ended, key=lambda e: e['end'])
            if now < last_ended['end'] + timedelta(minutes=gap_minutes):
                return last_ended
    upcoming = [e for e in live if e['start'] > now]
    if upcoming:
        return min(upcoming, key=lambda e: e['start'])
    return None


def sps_lookahead_pick(studio, today, token):
    """오늘 남은 생방송이 없을 때 앞으로 SPS_LOOKAHEAD_DAYS일 안에서 가장 이른
    다음 생방송을 찾는다. 그마저 없으면 None."""
    for days_ahead in range(1, SPS_LOOKAHEAD_DAYS + 1):
        date_str = (today + timedelta(days=days_ahead)).strftime('%Y-%m-%d')
        live = sps_parse_live(sps_fetch_repos(date_str, token), studio)
        if live:
            return min(live, key=lambda e: e['start'])
    return None


def sps_to_result(picked, now):
    """골라진 항목을 화면 반영용 형식으로 변환 — 시작이 now 기준 익일인지 계산해서
    컨트롤 페이지의 기존 "N"(익일) 표기를 그대로 재사용할 수 있게 한다."""
    start_dt, end_dt = picked['start'], picked['end']
    return {
        'eventId': picked['eventId'],
        'programName': picked['programName'],
        'videoSource': picked['videoSource'],
        'start_str': start_dt.strftime('%H:%M:%S'),
        'end_str': end_dt.strftime('%H:%M:%S'),
        'end_next_day': end_dt.date() != start_dt.date(),
        'start_next_day': start_dt.date() != now.date(),
    }


def sps_next_handoff_distance(all_live_today, now):
    """오늘 전체(스튜디오 무관) 생방송 중, 한 항목이 끝나고 SPS_HANDOFF_GAP 안에
    다음 항목이 바로 시작하는 "핸드오프" 경계들을 찾아서, now에서 가장 가까운
    경계까지 남은 시간을 반환(그 경계 안에 있으면 0). 그런 핸드오프가 오늘 아예
    없으면 None.
    모닝와이드1→2→3부(각각 다른 부조정실), 뉴스헌터스→8뉴스처럼 부조정실은 서로
    달라도 주조정실이 CM·ID로 몇 분 안에 바로 이어주는 구간을 촘촘하게 보기
    위함 — 오늘 마지막 생방송처럼 뒤에 몇 시간 비는 항목은 대상에서 빠진다."""
    ordered = sorted(all_live_today, key=lambda e: e['start'])
    boundaries = []
    for a, b in zip(ordered, ordered[1:]):
        gap = b['start'] - a['end']
        if timedelta(0) <= gap <= SPS_HANDOFF_GAP:
            boundaries.append(a['end'])
            boundaries.append(b['start'])
    if not boundaries:
        return None
    return min(abs(b - now) for b in boundaries)


def sps_find_preceding_handoff(all_live_today, picked):
    """picked 바로 앞에 SPS_HANDOFF_GAP 이내로 붙어있는 생방송(스튜디오 무관)이
    있으면 그 항목을, 없으면 None을 반환. 모닝와이드1→2→3부, 뉴스헌터스→8뉴스처럼
    앞 생방송이 실제로 끝나야 뒤 생방송 진입시간이 캐스케이드로 더 안 밀리고
    확정되는 관계를 찾기 위함."""
    candidates = [e for e in all_live_today if e['eventId'] != picked['eventId'] and e['end'] <= picked['start']]
    if not candidates:
        return None
    nearest = max(candidates, key=lambda e: e['end'])
    gap = picked['start'] - nearest['end']
    return nearest if timedelta(0) <= gap <= SPS_HANDOFF_GAP else None


def sps_listener():
    """SPS 운행표를 주기적으로 조회해서 생방송(liveOrVcr, 선택된 부조정실) 진행 항목을
    발견하면 생방송(카운트다운) 모드의 시작/종료 시각을 자동으로 채워준다. settings.json의
    spsAutoDetect를 false로 두면 즉시 끌 수 있다(스레드는 계속 돌되 아무것도 하지 않음).

    항목 식별은 (시작,종료,이름) 조합이 아니라 SPS의 eventId로 한다 — 종료시간은
    방송이 끝난 뒤 SPS가 실제값으로 사후 갱신해주는데(실측: 방송 종료 후 약 6~7초),
    이때 eventId는 그대로라서 "같은 방송의 시간이 갱신된 것"과 "다른 방송으로
    넘어간 것"을 구분할 수 있다. 같은 eventId를 추적하는 동안에도 시작/종료가 계속
    바뀔 수 있는데(앞선 방송이 늦게 끝나서 뒤 방송들이 줄줄이 밀리는 캐스케이드가
    하루 종일 일어남), broadcastTime/EndTime은 그때그때 최신값(=현재 SPS가 아는
    가장 정확한 값)으로 계속 갱신한다.

    onAirIndex(주조에서 실제로 내보내고 있는 항목의 인덱스, 실측 결과 진짜 스위칭과
    거의 동시(1~2초)에 반영됨)가 지금 추적 중인 eventId를 가리키는 순간을 "진짜
    라이브 전환"으로 보고 broadcastIsLive를 켠다. 이 전환이 처음 일어나는 그 순간의
    broadcastEndTime을 broadcastScheduledEndTime에 그대로 스냅샷으로 고정해둔다 —
    "방송 시작 전까지 계속 밀리던 예정이 실제로 시작하는 순간엔 이 값이었다"는
    기준점이라, 방송이 끝난 뒤 최종적으로 갱신되는 실제값과 나란히 비교할 수 있다
    (처음 발견한 시각에 고정하면 그 사이 캐스케이드로 여러 번 바뀐 몇 시간 전
    값이 돼버려서 비교 기준으로 의미가 없다).

    폴링 간격은 평상시엔 SPS_SLOW_INTERVAL이고, 오늘 생방송 중 서로 SPS_HANDOFF_GAP
    이내로 바로 이어지는 항목(모닝와이드1→2→3부, 뉴스헌터스→8뉴스처럼 부조정실은
    달라도 주조정실이 CM/ID로 몇 분 안에 이어주는 구간)의 경계 앞뒤 SPS_FAST_WINDOW
    안이면 SPS_FAST_INTERVAL로 촘촘해진다 — 오늘 마지막 생방송처럼 뒤에 몇 시간
    비는 항목의 종료는 굳이 그렇게까지 빨리 잡을 필요가 없어서 대상에서 빠진다.

    생방송 모드는 "다음 생방송까지 얼마나 남았는지"가 핵심이라, 오늘 마지막 방송이
    끝나도 내일 이후 첫 생방송을 계속 찾아서 카운트다운을 이어간다(시작이 오늘이
    아니면 broadcastTimeNextDay로 N 표시). SPS_LOOKAHEAD_DAYS 안에도 예정된 생방송이
    아예 없을 때만, 마지막으로 추적하던 카운트다운이 화면에 영원히 멈춰 있지 않도록
    일반(일정) 모드로 되돌린다.

    추적하던 eventId가 바뀌는(=그 방송이 끝난) 순간마다, 그때까지의 예정/실제
    종료·제작시간을 broadcast_history.jsonl에 한 줄 남긴다. spsGapMinutes가
    0(즉시 전환)이면 화면이 바로 다음 방송 대기로 넘어가 방금 끝난 방송 정보가
    사라지므로, 컨트롤 페이지에서 지난 방송들을 나중에 조회할 수 있게 하기 위함."""
    last_event_id = None
    last_was_live = False
    ever_live = False   # 지금 추적 중인 eventId가 한 번이라도 LIVE였는지(끝나면 다시 False로 안 꺼짐)
    last_video_source = None
    last_error = None
    startup = True   # 재시작 직후, 이미 추적 중이던 방송을 이어받을지 딱 한 번만 확인
    while True:
        interval = SPS_SLOW_INTERVAL
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                s_test = json.load(f)
            if s_test.get('testModeActive'):
                # 테스트 모드 중엔 실제 SPS 조회를 건너뛰고, 저장된 시작 시각이
                # 지났는지만 보고 broadcastIsLive를 흉내낸다. 테스트가 끝나는
                # 순간 실제 추적이 "새 방송 감지"로 제대로 다시 시작되도록
                # last_event_id 등을 계속 None으로 눌러둔다 — 그대로 두면(이전
                # 실제 eventId가 안 바뀐 걸로 보여서) 시간만 갱신되고
                # broadcastProgramName/EventId는 테스트 값에 그대로 물려 있는
                # 버그가 있었다.
                last_event_id = None
                last_was_live = False
                ever_live = False
                last_video_source = None
                now = datetime.now(KST)
                changed = False
                if not s_test.get('testModeEnded'):
                    try:
                        start_dt = datetime.strptime(s_test['broadcastTime'], '%H:%M:%S').replace(
                            year=now.year, month=now.month, day=now.day, tzinfo=KST)
                    except Exception:
                        start_dt = now
                    is_live_test = now >= start_dt
                    if s_test.get('broadcastIsLive') != is_live_test:
                        s_test['broadcastIsLive'] = is_live_test
                        changed = True
                    if is_live_test and not s_test.get('broadcastScheduledEndTime'):
                        s_test['broadcastScheduledEndTime'] = s_test.get('broadcastEndTime')
                        changed = True
                try:
                    expires = datetime.fromisoformat(s_test.get('testModeUntil'))
                except Exception:
                    expires = now
                if now >= expires:
                    s_test['testModeActive'] = False
                    s_test['testModeEnded'] = False
                    s_test['broadcastIsLive'] = False
                    changed = True
                if changed:
                    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                        json.dump(s_test, f)
                time.sleep(1)
                continue
            cfg = get_sps_settings()
            if cfg['enabled']:
                token = get_sps_token()
                if not token:
                    raise RuntimeError(
                        "sps_token.json 없음/만료 — 브라우저 있는 머신에서 "
                        "`uv run python setup/sps_authorize.py` 로 재발급 후 서버에 배치하세요."
                    )
                now = datetime.now(KST)
                today = sps_broadcast_date(now)
                date_str = today.strftime('%Y-%m-%d')
                repos = sps_fetch_repos(date_str, token)

                on_air_event_id = None
                try:
                    idx = sps_api_get(f'/daily-schedule/onairdate?date={date_str}', token).get('onAirIndex')
                    if idx is not None and 0 <= idx < len(repos):
                        on_air_event_id = repos[idx].get('eventId')
                except Exception:
                    pass

                all_live_today = sps_parse_live(repos, '')
                studio_live_today = [e for e in all_live_today if not cfg['studio'] or e['videoSource'] == cfg['studio']]

                picked = sps_pick_from_live(studio_live_today, now, cfg['gap_minutes'], on_air_event_id)
                if not picked:
                    picked = sps_lookahead_pick(cfg['studio'], today, token)

                if picked:
                    result = sps_to_result(picked, now)
                    is_live = on_air_event_id is not None and on_air_event_id == result['eventId']

                    # 앞에 SPS_HANDOFF_GAP 이내로 바로 붙는 생방송이 없으면(오늘 첫 방송
                    # 등) 캐스케이드로 당장 흔들릴 상대가 없으니 처음부터 확정으로 본다.
                    # 있으면, 그 앞 생방송이 실제로 끝나야(=onAirIndex가 더 이상 그걸
                    # 가리키지 않아야) 확정 — 그 전까지는 계속 밀릴 수 있는 잠정값이다.
                    preceding = sps_find_preceding_handoff(all_live_today, picked)
                    start_confirmed = True
                    if preceding is not None:
                        start_confirmed = False
                        if on_air_event_id is not None:
                            on_air_entry = next((e for e in repos if e.get('eventId') == on_air_event_id), None)
                            if on_air_entry:
                                try:
                                    on_air_start = datetime.strptime(on_air_entry['startTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
                                    start_confirmed = on_air_start >= preceding['end']
                                except Exception:
                                    pass

                    with open(SETTINGS_FILE, encoding='utf-8') as f:
                        s = json.load(f)
                    changed = False
                    if startup:
                        startup = False
                        if s.get('broadcastEventId') == result['eventId']:
                            # 서버 재시작 전에 이미 추적 중이던 것과 같은 방송 — 방금 처음
                            # 라이브로 전환된 걸로 착각해 예정종료시각을 재시작 시점 값으로
                            # 다시 스냅샷 뜨지 않도록, 하던 상태를 그대로 이어받는다.
                            last_event_id = result['eventId']
                            last_was_live = bool(s.get('broadcastIsLive'))
                            ever_live = last_was_live or bool(s.get('broadcastScheduledEndTime'))
                            last_video_source = result['videoSource']
                    if s.get('broadcastStartConfirmed') != start_confirmed:
                        s['broadcastStartConfirmed'] = start_confirmed
                        changed = True
                    if result['eventId'] != last_event_id:
                        if last_event_id is not None:
                            old_start, old_end = s.get('broadcastTime'), s.get('broadcastEndTime')
                            dur = None
                            if old_start and old_end:
                                try:
                                    st = datetime.strptime(old_start, '%H:%M:%S')
                                    en = datetime.strptime(old_end, '%H:%M:%S')
                                    if s.get('broadcastEndTimeNextDay'):
                                        en += timedelta(days=1)
                                    dur = (en - st).total_seconds()
                                except Exception:
                                    dur = None
                            sps_append_history({
                                'date': today.strftime('%Y-%m-%d'),
                                'eventId': last_event_id,
                                'programName': s.get('broadcastProgramName') or '',
                                'videoSource': last_video_source,
                                'scheduledEnd': s.get('broadcastScheduledEndTime') or '',
                                'actualStart': old_start or '',
                                'actualEnd': old_end or '',
                                'actualDurationSec': dur,
                                'wasLive': ever_live,
                                'recordedAt': now.isoformat(),
                            })
                        s['countdownMode'] = True
                        s['broadcastEventId'] = result['eventId']
                        s['broadcastTime'] = result['start_str']
                        s['broadcastTimeNextDay'] = result['start_next_day']
                        s['broadcastEndTime'] = result['end_str']
                        s['broadcastEndTimeNextDay'] = result['end_next_day']
                        s['broadcastScheduledEndTime'] = ''
                        s['broadcastScheduledEndTimeNextDay'] = False
                        s['broadcastProgramName'] = result['programName']
                        s['broadcastVideoSource'] = result['videoSource']
                        last_event_id = result['eventId']
                        last_video_source = result['videoSource']
                        last_was_live = False
                        ever_live = False
                        changed = True
                        when = f"{'내일 이후 ' if result['start_next_day'] else ''}{result['programName']} {result['start_str']} ~ {result['end_str']}"
                        print(f"[SPS] 생방송 감지 → {when} 자동 반영", flush=True)
                    elif (s.get('broadcastTime') != result['start_str'] or s.get('broadcastEndTime') != result['end_str']
                          or s.get('broadcastTimeNextDay') != result['start_next_day']
                          or s.get('broadcastEndTimeNextDay') != result['end_next_day']):
                        old_start, old_end = s.get('broadcastTime'), s.get('broadcastEndTime')
                        s['broadcastTime'] = result['start_str']
                        s['broadcastTimeNextDay'] = result['start_next_day']
                        s['broadcastEndTime'] = result['end_str']
                        s['broadcastEndTimeNextDay'] = result['end_next_day']
                        changed = True
                        print(f"[SPS] 시간 갱신(실제반영/캐스케이드) → {result['programName']} "
                              f"{old_start}~{old_end} → {result['start_str']}~{result['end_str']}", flush=True)
                    if is_live and not last_was_live:
                        s['broadcastScheduledEndTime'] = s['broadcastEndTime']
                        s['broadcastScheduledEndTimeNextDay'] = s['broadcastEndTimeNextDay']
                        changed = True
                        print(f"[SPS] LIVE 전환 → {result['programName']} 예정 종료 {s['broadcastEndTime']} 스냅샷 고정", flush=True)
                    if s.get('broadcastIsLive') != is_live:
                        s['broadcastIsLive'] = is_live
                        changed = True
                    if is_live:
                        ever_live = True
                    last_was_live = is_live
                    if changed:
                        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                            json.dump(s, f)
                elif last_event_id is not None:
                    with open(SETTINGS_FILE, encoding='utf-8') as f:
                        s = json.load(f)
                    old_start, old_end = s.get('broadcastTime'), s.get('broadcastEndTime')
                    dur = None
                    if old_start and old_end:
                        try:
                            st = datetime.strptime(old_start, '%H:%M:%S')
                            en = datetime.strptime(old_end, '%H:%M:%S')
                            if s.get('broadcastEndTimeNextDay'):
                                en += timedelta(days=1)
                            dur = (en - st).total_seconds()
                        except Exception:
                            dur = None
                    sps_append_history({
                        'date': today.strftime('%Y-%m-%d'),
                        'eventId': last_event_id,
                        'programName': s.get('broadcastProgramName') or '',
                        'videoSource': last_video_source,
                        'scheduledEnd': s.get('broadcastScheduledEndTime') or '',
                        'actualStart': old_start or '',
                        'actualEnd': old_end or '',
                        'actualDurationSec': dur,
                        'wasLive': ever_live,
                        'recordedAt': now.isoformat(),
                    })
                    if s.get('countdownMode'):
                        s['countdownMode'] = False
                        s['broadcastTime'] = ''
                        s['broadcastEndTime'] = ''
                        s['broadcastProgramName'] = ''
                        s['broadcastVideoSource'] = ''
                        s['broadcastScheduledEndTime'] = ''
                        s['broadcastIsLive'] = False
                        s['broadcastStartConfirmed'] = True
                        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                            json.dump(s, f)
                        print(f"[SPS] 앞으로 {SPS_LOOKAHEAD_DAYS}일 안에 예정된 생방송 없음 → 일반 모드로 전환", flush=True)
                    last_event_id = None
                    last_video_source = None
                    last_was_live = False
                    ever_live = False

                # 핸드오프 근처뿐 아니라, 지금 화면에 보여주는 방송 자체의 시작/종료
                # 앞뒤로도 촘촘하게 돈다 — 안 그러면 앞뒤가 몇 시간씩 비어 핸드오프
                # 대상이 아닌 방송(예: 12시뉴스)은 평상시 간격(20초)만 적용돼서
                # onAirIndex가 이미 넘어갔어도 LIVE 뱃지가 최대 20초까지 늦게 붙는다.
                dist = sps_next_handoff_distance(all_live_today, now)
                if picked is not None:
                    own_dist = min(abs(picked['start'] - now), abs(picked['end'] - now))
                    dist = own_dist if dist is None else min(dist, own_dist)
                interval = SPS_FAST_INTERVAL if (dist is not None and dist <= SPS_FAST_WINDOW) else SPS_SLOW_INTERVAL
            last_error = None
        except Exception as e:
            msg = str(e)
            if msg != last_error:
                print(f"[SPS] 오류: {msg}", flush=True)
                last_error = msg
        time.sleep(interval)


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
def closest_time_occurrence(now, hhmmss):
    """'HH:MM:SS'는 날짜가 없는 시각이라 now.date()에 그대로 앉히면, 자정 전에
    시작해 자정을 넘겨 진행 중인 방송을 검사할 때 실제 종료 시각(어제 날짜 기준)이
    아니라 "오늘 그 시각"(아직 한참 남은 미래)으로 잘못 계산된다 — 예를 들어
    23:00~01:00(N) 방송을 23:30에 검사하면 종료 01:00이 now.date()에 앉혀져
    '오늘 새벽 1시(=22시간 전, 이미 지남)'로 오판되어 방송 시작 직후 자동전환이
    잘못 발동한다. 어제/오늘/내일 중 now와 가장 가까운 occurrence를 선택해 방지."""
    t = datetime.strptime(hhmmss, '%H:%M:%S').time()
    candidates = [datetime.combine(now.date() + timedelta(days=d), t, tzinfo=KST) for d in (-1, 0, 1)]
    return min(candidates, key=lambda c: abs((c - now).total_seconds()))


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
                end_dt = closest_time_occurrence(now, end_val)
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
    API를 다시 호출하지 않고 캐시만 읽는다. 이미 끝난 항목은 broadcast_history.jsonl과
    eventId로 대조해서 실제 시작/종료/제작시간도 같이 붙여준다 — 캐시의 startTime/
    duration 자체는 시간이 지나며 실제값으로 계속 갱신되므로 "예정"과 "실제"를
    나란히 비교하려면 LIVE 전환 시점에 스냅샷해둔 history 기록이 따로 필요하다."""
    if not session.get('control_authed'):
        return ('', 403)
    date_str = sps_broadcast_date(datetime.now(KST)).strftime('%Y-%m-%d')
    path = os.path.join(SPS_DATA_DIR, f'sps_schedule_{date_str}.json')
    try:
        with open(path, encoding='utf-8') as f:
            repos = json.load(f).get('repos', [])
    except Exception:
        repos = []
    history = sps_read_history(date_str)

    studio = get_sps_settings()['studio']
    items = []
    for entry in repos:
        if not entry.get('liveOrVcr'):
            continue
        if studio and entry.get('videoSource') != studio:
            continue
        start_dt = datetime.strptime(entry['startTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
        end_dt = start_dt + timedelta(seconds=entry['duration'])
        item = {
            'eventId': entry.get('eventId'),
            'start': start_dt.strftime('%H:%M:%S'),
            'end': end_dt.strftime('%H:%M:%S'),
            'duration': entry['duration'],
            'videoSource': entry.get('videoSource', ''),
            'programName': entry.get('programName', ''),
        }
        rec = history.get(entry.get('eventId'))
        if rec:
            # 캐시의 end는 시간이 지나며 실제값으로 계속 갱신되므로, 이미 방송된 항목은
            # 주 표시(item['end'])를 LIVE 전환 시점에 고정해 둔 예정값으로 되돌리고
            # 실제값은 actualEnd에만 담는다 — 예정 자리 숫자가 실제로 바뀌어 보이지
            # 않고, 실제값은 옆에 별도로 표시된다.
            if rec.get('wasLive') and rec.get('scheduledEnd'):
                item['end'] = rec['scheduledEnd']
            item['actualStart'] = rec.get('actualStart') or ''
            item['actualEnd'] = rec.get('actualEnd') or ''
            item['actualDurationSec'] = rec.get('actualDurationSec')
            item['wasLive'] = bool(rec.get('wasLive'))
        items.append(item)
    return jsonify(items)


@app.route('/sps/studios')
def sps_studios():
    """오늘 운행표 캐시에 실제로 등장하는 생방송 스튜디오(videoSource) 목록을
    중복 없이 정렬해서 반환 — TS-1~7뿐 아니라 RSW(상암/등촌 등 TS 외 부조정실)처럼
    미리 알 수 없는 코드도 컨트롤 페이지 드롭다운에 하드코딩 없이 자동으로 뜨게
    하기 위함. 코드값을 그대로 쓰므로 별도 사람이 읽기 좋은 이름은 붙이지 않는다."""
    if not session.get('control_authed'):
        return ('', 403)
    date_str = sps_broadcast_date(datetime.now(KST)).strftime('%Y-%m-%d')
    path = os.path.join(SPS_DATA_DIR, f'sps_schedule_{date_str}.json')
    try:
        with open(path, encoding='utf-8') as f:
            repos = json.load(f).get('repos', [])
    except Exception:
        repos = []
    sources = sorted({e.get('videoSource', '') for e in repos if e.get('liveOrVcr') and e.get('videoSource')})
    return jsonify(sources)


@app.route('/sps/history')
def sps_history():
    """지난 생방송들의 예정/실제 시작·종료·제작시간 기록(broadcast_history.jsonl)을
    반환. spsGapMinutes가 0(즉시 전환)이면 방송 화면이 끝나자마자 바로 다음 방송
    대기로 넘어가서 방금 끝난 방송 정보가 화면에서 사라지므로, 나중에 "그 방송이
    언제 끝났고 제작시간이 얼마였는지" 확인할 수 있도록 컨트롤 페이지에서 조회."""
    if not session.get('control_authed'):
        return ('', 403)
    date_str = request.args.get('date') or sps_broadcast_date(datetime.now(KST)).strftime('%Y-%m-%d')
    path = os.path.join(SPS_DATA_DIR, 'broadcast_history.jsonl')
    records = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get('date') == date_str:
                    records.append(rec)
    except Exception:
        pass
    records.sort(key=lambda r: r.get('actualStart') or '', reverse=True)
    return jsonify(records)


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
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                old_settings = json.load(f)
        except Exception:
            old_settings = {}
        # 이 필드들은 sps_listener만 채우는 서버 파생 필드다 — 컨트롤 페이지엔 이걸 위한
        # 수동 입력 UI가 없으므로, 오래된/캐시된 페이지가 이 필드를 모른 채(또는 값이
        # 비어 있는 채로) 저장해도 덮어써지지 않도록 항상 기존 값을 그대로 유지한다.
        for key in ('broadcastProgramName', 'broadcastVideoSource', 'broadcastEventId', 'broadcastIsLive',
                    'broadcastScheduledEndTime', 'broadcastScheduledEndTimeNextDay',
                    'broadcastStartConfirmed'):
            new_settings[key] = old_settings.get(key)
        # SPS 자동모드가 켜진 상태에서는 sps_listener가 방송 시간의 유일한 소스여야 한다.
        # 컨트롤 페이지는 최초 로드 이후 이 필드들을 재폴링하지 않으므로, 페이지를 오래
        # 띄워두고 스튜디오 변경 등 다른 설정만 저장해도 오래된 시간값이 그대로 다시
        # 전송돼 sps_listener가 이미 감지해 둔 최신 시간을 덮어써버릴 수 있다 — 그러면
        # 프로그램명(보호됨)과 시간(덮어써짐)이 서로 다른 방송의 값으로 뒤섞인다.
        # 자동모드 중엔 이 네 필드도 broadcastProgramName처럼 기존 값을 그대로 유지한다.
        if new_settings.get('spsAutoDetect', old_settings.get('spsAutoDetect', True)):
            for key in ('broadcastTime', 'broadcastEndTime',
                        'broadcastTimeNextDay', 'broadcastEndTimeNextDay'):
                new_settings[key] = old_settings.get(key, new_settings.get(key))
        # 화면 하단 표시는 countdownMode/stopwatchMode 중 하나만 켜져 있다고 가정하고
        # 렌더링한다(둘 다 true면 생방송 위젯과 타이머/스톱워치 위젯이 동시에 표시됨).
        # 컨트롤 페이지 UI는 세그먼트 버튼 하나로 배타적으로 저장하지만, 다른 경로로
        # 두 값이 함께 true가 되는 걸 막기 위해 서버에서도 강제로 배타성을 지킨다 —
        # 나중에 저장된 쪽(요청에 명시적으로 true로 온 쪽)을 우선한다.
        if new_settings.get('countdownMode') and new_settings.get('stopwatchMode'):
            if old_settings.get('countdownMode'):
                new_settings['countdownMode'] = False
            else:
                new_settings['stopwatchMode'] = False
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


@app.route('/test/countdown/start', methods=['POST'])
def test_countdown_start():
    """방송 카운트다운 UI(대기→시작→LIVE→예정초과)를 실제 SPS 방송을 기다리지
    않고 짧은 시간 안에 재현해보기 위한 테스트 모드. 실행 중엔 sps_listener가
    실제 SPS 조회를 건너뛰고 이 값들만 시간에 따라 흉내낸다(history 기록은
    남기지 않음)."""
    if not session.get('control_authed'):
        return ('', 403)
    body = request.get_json(force=True) or {}
    try:
        start_in_sec = max(0, int(body.get('startInSec', 60)))
        duration_sec = max(1, int(body.get('durationSec', 90)))
    except (TypeError, ValueError):
        return ('', 400)
    now = datetime.now(KST)
    start = now + timedelta(seconds=start_in_sec)
    end = start + timedelta(seconds=duration_sec)
    with open(SETTINGS_FILE, encoding='utf-8') as f:
        s = json.load(f)
    s['countdownMode'] = True
    s['stopwatchMode'] = False
    s['broadcastEventId'] = f'TEST-{int(now.timestamp())}'
    s['broadcastProgramName'] = '[테스트] 방송'
    s['broadcastVideoSource'] = 'TEST'
    s['broadcastTime'] = start.strftime('%H:%M:%S')
    s['broadcastTimeNextDay'] = False
    s['broadcastEndTime'] = end.strftime('%H:%M:%S')
    s['broadcastEndTimeNextDay'] = False
    s['broadcastScheduledEndTime'] = ''
    s['broadcastScheduledEndTimeNextDay'] = False
    s['broadcastIsLive'] = False
    s['broadcastStartConfirmed'] = True
    s['testModeActive'] = True
    s['testModeEnded'] = False
    s['testModeUntil'] = (end + timedelta(minutes=10)).isoformat()
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(s, f)
    return jsonify({'start': s['broadcastTime'], 'end': s['broadcastEndTime']})


@app.route('/test/countdown/end', methods=['POST'])
def test_countdown_end():
    """지금 이 순간을 '실제 종료 시각'으로 확정 — 예정종료를 넘긴 채로 두면
    실제/제작시간 비교 표시가 뜨는지까지 이어서 확인할 수 있다."""
    if not session.get('control_authed'):
        return ('', 403)
    with open(SETTINGS_FILE, encoding='utf-8') as f:
        s = json.load(f)
    if not s.get('testModeActive'):
        return ('', 409)
    now = datetime.now(KST)
    s['broadcastIsLive'] = False
    s['broadcastEndTime'] = now.strftime('%H:%M:%S')
    s['broadcastEndTimeNextDay'] = False
    s['testModeEnded'] = True
    s['testModeUntil'] = (now + timedelta(seconds=20)).isoformat()
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(s, f)
    return ('', 204)


@app.route('/test/countdown/stop', methods=['POST'])
def test_countdown_stop():
    """테스트 중단 — 다음 폴링 때 sps_listener가 실제 SPS 상태로 되돌린다."""
    if not session.get('control_authed'):
        return ('', 403)
    with open(SETTINGS_FILE, encoding='utf-8') as f:
        s = json.load(f)
    s['testModeActive'] = False
    s['testModeEnded'] = False
    s['broadcastIsLive'] = False
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(s, f)
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
