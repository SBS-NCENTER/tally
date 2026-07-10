import pytest
import tally_server


def test_get_calendar_service_raises_without_token(monkeypatch, tmp_path):
    # 유효 토큰이 없으면 대화형 OAuth(run_local_server, headless서 hang)로 빠지지 않고 즉시 raise
    monkeypatch.setattr(tally_server, 'TOKEN_FILE', str(tmp_path / 'nope.json'))
    monkeypatch.setattr(tally_server, '_creds', None)
    with pytest.raises(RuntimeError):
        tally_server.get_calendar_service()
