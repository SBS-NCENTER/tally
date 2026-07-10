import tally_server


def test_parse_valid_line():
    line = "NOTIFY set MIXER:Current/InCh/Fader/Level 0 0 -1000"
    assert tally_server.parse(line) == (0, -1000)


def test_parse_channel_out_of_range():
    line = "NOTIFY set MIXER:Current/InCh/Fader/Level 9 0 -1000"
    assert tally_server.parse(line) == (None, None)


def test_parse_non_matching_line():
    assert tally_server.parse("garbage") == (None, None)


def test_calc_on_air_below_threshold_is_false():
    tally_server.fader = {i: -32768 for i in range(8)}
    assert tally_server.calc_on_air() is False


def test_calc_on_air_above_threshold_is_true():
    tally_server.fader = {i: -32768 for i in range(8)}
    tally_server.fader[3] = -5000  # THRESHOLD=-8000 보다 큼
    assert tally_server.calc_on_air() is True
