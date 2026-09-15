"""Offline UART regression checks; run with Python's built-in unittest."""

from collections import deque
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openflight_bench.capture import BenchController
from openflight_bench.wire import HEADER, TEMP, DESC, read_dump


FOOTER = b"Done\nl3dump:/>"


def dump_bytes(payload=None):
    if payload is None:
        payload = bytes(range(256)) * 144
    assert len(payload) == 36864
    header = HEADER.pack(b"ILD1", 7, 64, 36, 3, 4, 1, 4, 0, 0, 10000)
    metadata = b"".join(DESC.pack(0, 1, 10000 if i else 0) for i in range(64))
    return header + bytes(TEMP.size) + metadata + payload


class SerialReplay:
    def __init__(self, chunks):
        self.chunks = deque(bytearray(c) for c in chunks if c)
        self.writes = []

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, count=1):
        if not self.chunks:
            return b""
        result = bytes(self.chunks[0][:count])
        del self.chunks[0][:count]
        if not self.chunks[0]:
            self.chunks.popleft()
        return result

    def write_line(self, line):
        self.writes.append(line)

    def close(self):
        pass


class VirtualClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        self.value += 0.000001
        return self.value


def replay(chunks):
    with patch("openflight_bench.wire.time.monotonic", VirtualClock()):
        return read_dump(SerialReplay(chunks), timeout=1, stall_timeout=.001)


class UartReaderTests(unittest.TestCase):
    def test_every_footer_split_preserves_shortfall_and_wire(self):
        binary = dump_bytes()[:-512]
        for footer in (FOOTER, b"Done\r\nl3dump:/>"):
            for cut in range(len(footer) + 1):
                with self.subTest(footer=footer, cut=cut):
                    result = replay([binary + footer[:cut], footer[cut:]])
                    self.assertEqual(result["raw"], binary)
                    self.assertEqual(result["short_by"], 512)
                    self.assertEqual(result["wire_bytes"], binary + footer)
                    self.assertTrue(result["completion_seen"])
                    self.assertEqual(result["status"], "rejected_short")

    def test_single_byte_reads_preserve_shortfall(self):
        binary = dump_bytes()[:-512]
        wire = b"l3dump\n" + binary + FOOTER
        result = replay([wire[i:i+1] for i in range(len(wire))])
        self.assertEqual(result["raw"], binary)
        self.assertEqual(result["wire_bytes"], wire)
        self.assertEqual(result["short_by"], 512)

    def test_marker_inside_payload_is_not_truncated(self):
        payload = bytearray(36864)
        payload[256:256+len(FOOTER)] = FOOTER
        payload[1024:1028] = b"Done"
        binary = dump_bytes(bytes(payload))
        split = 300 + 256 + len(FOOTER)
        result = replay([binary[:split], binary[split:], FOOTER])
        self.assertEqual(result["raw"], binary)
        self.assertEqual(result["status"], "complete")

    def test_complete_payload_and_preamble_are_preserved(self):
        binary = dump_bytes()
        wire = b"l3dump\n" + binary + FOOTER
        result = replay([wire])
        self.assertEqual(result["raw"], binary)
        self.assertEqual(result["wire_bytes"], wire)
        self.assertEqual(result["short_by"], 0)
        self.assertEqual(result["status"], "complete")

    def test_extra_binary_is_rejected(self):
        binary = dump_bytes()
        result = replay([binary + b"XX" + FOOTER])
        self.assertEqual(result["raw"], binary + b"XX")
        self.assertEqual(result["status"], "rejected_extra")
        self.assertEqual(result["extra_bytes"], 2)

    def test_missing_footer_does_not_invent_bytes(self):
        binary = dump_bytes()[:-512]
        result = replay([binary])
        self.assertEqual(result["raw"], binary)
        self.assertEqual(result["short_by"], 512)
        self.assertFalse(result["completion_seen"])

    def test_complete_by_length_without_footer(self):
        binary = dump_bytes()
        result = replay([binary])
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["completion_seen"])

    def test_firmware_error_survives_and_stops_at_prompt(self):
        wire = b"l3dump\nError: HWA post-trigger frame freeze timed out\nl3dump:/>"
        with self.assertRaisesRegex(RuntimeError, "HWA post-trigger frame freeze timed out") as context:
            replay([wire[:12], wire[12:]])
        self.assertEqual(context.exception.wire_bytes, wire)

    def test_partial_header_is_recorded(self):
        wire = b"l3dump\nILD1\x07\x00"
        with self.assertRaisesRegex(TimeoutError, "header") as context:
            replay([wire])
        self.assertEqual(context.exception.wire_bytes, wire)

    def test_capture_saves_wire_and_failure_metadata(self):
        wire = b"Error: HWA post-trigger frame freeze timed out\nl3dump:/>"
        fake = SerialReplay([wire])
        with tempfile.TemporaryDirectory() as folder, \
             patch("openflight_bench.capture.BenchSerial", return_value=fake), \
             patch.object(BenchController, "drain_text", return_value=""):
            controller = BenchController("COM7", Path(folder), timeout=1)
            controller.applied = True
            with self.assertRaisesRegex(RuntimeError, "UART record saved"):
                controller.capture("failed")
            saved = next(controller.out_dir.glob("*.wire.bin"))
            self.assertEqual(saved.read_bytes(), wire)
            entry = controller.manifest["captures"][0]
            data = json.loads((controller.out_dir / entry["sidecar"]).read_text())
            self.assertFalse(data["accepted_for_analysis"])
            self.assertIn("freeze timed out", data["error"])

    def test_capture_saves_short_binary_and_original_wire(self):
        binary = dump_bytes()[:-512]
        wire = b"l3dump\n" + binary + FOOTER
        fake = SerialReplay([wire])
        with tempfile.TemporaryDirectory() as folder, \
             patch("openflight_bench.capture.BenchSerial", return_value=fake), \
             patch.object(BenchController, "drain_text", return_value=""), \
             patch("openflight_bench.wire.time.monotonic", VirtualClock()):
            controller = BenchController("COM7", Path(folder), timeout=1)
            controller.applied = True
            result = controller.capture("short")
            self.assertEqual(result["path"].read_bytes(), binary)
            self.assertEqual((controller.out_dir / result["wire_file"]).read_bytes(), wire)
            self.assertEqual(result["capture_info"]["short_by"], 512)
            self.assertFalse(result["accepted_for_analysis"])
            self.assertEqual(fake.writes, ["l3dump", "stats"])


if __name__ == "__main__":
    unittest.main()
