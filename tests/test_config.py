import tally_server


def test_load_server_config_reads_values(tmp_path):
    toml = tmp_path / "server.toml"
    toml.write_text(
        '[[service]]\nname = "tally-ts6"\nhost = "127.0.0.1"\nport = 5006\n'
    )
    assert tally_server.load_server_config(str(toml)) == {
        "name": "tally-ts6",
        "host": "127.0.0.1",
        "port": 5006,
    }


def test_load_server_config_defaults_when_missing(tmp_path):
    assert tally_server.load_server_config(str(tmp_path / "nope.toml")) == {
        "name": "tally",
        "host": "0.0.0.0",
        "port": 5005,
    }


def test_load_server_config_malformed_falls_back(tmp_path):
    toml = tmp_path / "server.toml"
    toml.write_text('[service]\nname = "oops"\n')  # singular table (should be [[service]]) → dict, not list
    assert tally_server.load_server_config(str(toml)) == {
        "name": "tally",
        "host": "0.0.0.0",
        "port": 5005,
    }
