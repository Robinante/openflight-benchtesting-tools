from pathlib import Path
import sys
import re
from unittest.mock import patch
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from openflight_bench.config import BenchProfile, TX_MODES, make_config
from openflight_bench.capture import BenchController
from openflight_bench.cli import parse_value
from openflight_bench.wire import inspect_bytes, read_dump


def test_default_profile_is_the_new_baseline():
    profile = BenchProfile()
    text = make_config(profile)
    assert profile.packed_tx_backoff == 394758
    assert "profileCfg 0 60.0 7 3 38 394758 0 100 1 128 4000 0 0 24" in text
    assert profile.name == "baseline_rx24_tx6_hpf00_p10_f5"
    assert "captureCfg 0 43 0 43 0 5 1" in text
    assert "frameCfg 0 2 12 0 10 1 0" in text


def test_frame_period_is_generated_and_settable():
    profile = BenchProfile(frame_period_ms=5)
    assert "frameCfg 0 2 12 0 5 1 0" in make_config(profile)
    assert parse_value(BenchProfile(), "period", "5").frame_period_ms == 5
    assert parse_value(BenchProfile(), "frame", "5").frame_period_ms == 5


def test_frame_period_requires_whole_positive_milliseconds():
    with pytest.raises(ValueError):
        BenchProfile(frame_period_ms=0)
    with pytest.raises(ValueError):
        BenchProfile(frame_period_ms=2.5)


def test_profile_rejects_firmware_freeze_timeout():
    with pytest.raises(ValueError, match="30 frames.*10 ms = 300 ms"):
        BenchProfile(post_frames=30, frame_period_ms=10)

    profile = BenchProfile(post_frames=5, frame_period_ms=10)
    assert profile.post_frames * profile.post_stride * profile.frame_period_ms == 50


def test_all_tx_variants_keep_three_chirp_indices():
    expected = {
        "all": (1, 2, 4), "off": (0, 0, 0), "tx0": (1, 0, 0),
        "tx1": (0, 2, 0), "tx2": (0, 0, 4), "tx02": (1, 0, 4),
    }
    for mode, masks in expected.items():
        text = make_config(BenchProfile(tx_mode=mode))
        got = tuple(int(re.search(rf"^chirpCfg {i} .* (\d+)$", text, re.MULTILINE).group(1)) for i in range(3))
        assert got == masks


def test_supplied_dumps_are_gated_by_length():
    paths = sorted(Path(__file__).parents[1].joinpath("upload").glob("raw_couch_rxgain_sweep_1_*.l3dump"))
    # Historical captures are intentionally excluded from clean clones.
    if not paths:
        pytest.skip("optional uploaded raw-dump fixtures are not present")
    # The uploaded raw dumps are deliberately excluded from the repository.
    # When they are available locally, validate their known classifications;
    # a clean clone should still have a runnable test suite.
    if not paths:
        return
    statuses = [inspect_bytes(path.read_bytes())["status"] for path in paths]
    assert statuses == ["reject", "reject", "reject", "complete", "complete"]


class FakeSerial:
    def __init__(self, data, *, port=None, baudrate=None):
        self.data = bytearray(data)
        self.writes = []
        self.port = port
        self.baudrate = baudrate

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, count=1):
        if not self.data:
            return b""
        count = min(count, len(self.data))
        result = bytes(self.data[:count])
        del self.data[:count]
        return result

    def write_line(self, line):
        self.writes.append(line)

    def close(self):
        pass


def test_reader_strips_completion_text_and_rejects_short_payload():
    path = Path(__file__).parents[1] / "upload/raw_couch_rxgain_sweep_1_20260910_200150_r4_g0004.l3dump"
    if not path.exists():
        pytest.skip("optional uploaded raw-dump fixture is not present")
    if not path.exists():
        return
    raw = path.read_bytes()
    complete = read_dump(FakeSerial(raw + b"Done\nl3dump:/>"), timeout=.2, stall_timeout=.01)
    assert complete["status"] == "complete"
    assert complete["raw"] == raw

    short = raw[:-1024]
    rejected = read_dump(FakeSerial(short + b"Done\nl3dump:/>"), timeout=.2, stall_timeout=.01)
    assert rejected["status"] == "rejected_short"
    assert rejected["actual_bytes"] == len(short)

    extra = read_dump(FakeSerial(raw + b"XXDone\nl3dump:/>"), timeout=.2, stall_timeout=.01)
    assert extra["status"] == "rejected_extra"
    assert extra["extra_bytes"] == 2


def test_capture_triggers_firmware_dump_before_reading(tmp_path):
    source = Path(__file__).parents[1] / "upload/raw_couch_rxgain_sweep_1_20260910_200150_r4_g0004.l3dump"
    if not source.exists():
        pytest.skip("optional uploaded raw-dump fixture is not present")
    if not source.exists():
        return
    fake = FakeSerial(source.read_bytes() + b"Done\nl3dump:/>")
    with patch("openflight_bench.capture.BenchSerial", return_value=fake), \
         patch.object(BenchController, "drain_text", return_value=""):
        controller = BenchController("COM7", tmp_path, timeout=.2)
        controller.applied = True
        result = controller.capture("trigger_test")
        controller.close()
    assert result["accepted_for_analysis"]
    assert fake.writes[0] == "l3dump"
