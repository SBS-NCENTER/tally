import tally_server


def test_health_returns_ok():
    client = tally_server.app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_write_pidfile_writes_pid(tmp_path):
    pid_path = tmp_path / "data" / "tally.pid"
    tally_server.write_pidfile(str(pid_path))
    assert pid_path.read_text().strip().isdigit()
