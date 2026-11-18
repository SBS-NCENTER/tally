from datetime import datetime

import tally_server
from tally_server import KST


def _picked(start, end):
    return {'eventId': 'e1', 'programName': '나이트 라인', 'videoSource': 'TS-4',
            'start': start, 'end': end}


START = datetime(2026, 11, 12, 23, 58, 35, tzinfo=KST)
END = datetime(2026, 11, 13, 0, 23, 30, tzinfo=KST)


def test_before_midnight_start_is_today():
    r = tally_server.sps_to_result(_picked(START, END), datetime(2026, 11, 12, 23, 59, tzinfo=KST))
    assert r['start_next_day'] is False
    assert r['end_next_day'] is True


def test_after_midnight_yesterday_start_is_not_next_day():
    # 자정 전에 시작해 자정을 넘겨 진행 중인 방송 — 시작은 '어제'지 '내일'이 아니다
    r = tally_server.sps_to_result(_picked(START, END), datetime(2026, 11, 13, 0, 0, 5, tzinfo=KST))
    assert r['start_next_day'] is False
    assert r['end_next_day'] is True


def test_tomorrow_start_is_next_day():
    # 자정 이후 시작 방송을 전날 밤에 볼 때(예: 00:17 나이트라인을 23:00에)
    start = datetime(2026, 11, 18, 0, 17, 44, tzinfo=KST)
    end = datetime(2026, 11, 18, 0, 42, 26, tzinfo=KST)
    r = tally_server.sps_to_result(_picked(start, end), datetime(2026, 11, 17, 23, 0, tzinfo=KST))
    assert r['start_next_day'] is True
    assert r['end_next_day'] is False
