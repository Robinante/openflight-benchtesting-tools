"""Content verification for captures.

The bench capture tool records integrity information in three places, and this
module reads all three rather than inventing a fourth:

``<capture>.json`` sidecar (schema with ``file_sha256``)
    ``file_sha256``   sha256 of the .l3dump
    ``wire_sha256``   sha256 of the .wire.bin, the untrimmed received stream
    ``wire_file`` / ``wire_bytes``
    ``config_sha256`` sha256 of the .cfg that was applied
    ``accepted_for_analysis``  the capture tool's own verdict
    ``capture_info.status`` / ``expected_bytes`` / ``actual_bytes`` /
    ``extra_bytes`` / ``completion_seen``

``SHA256SUMS`` in the capture directory
    Standard ``sha256sum -c`` format, one line per file. Covers captures whose
    sidecar predates ``file_sha256``.

``session_manifest.json`` in the capture directory
    Per-capture status and the applied profile, plus ``config_sha256`` per
    named profile.

ILD1 v7 itself carries no checksum, so none of this comes from the dump bytes.
A v8 that embedded a payload hash would let a reader detect corruption without
a sidecar; until then verification is external, which is exactly what these
files provide.

Nothing here rejects a capture on its own. It records what was found;
``Capture.integrity`` surfaces it and the caller decides.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SHA256SUMS_NAME = "SHA256SUMS"
SESSION_MANIFEST_NAME = "session_manifest.json"
_CHUNK = 1 << 20


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=64)
def read_sha256sums(directory) -> dict:
    """Parse a SHA256SUMS file into {filename: digest}. Empty if absent.

    Cached per directory: a session is hundreds of captures all pointing at the
    same file, and re-parsing it per capture is pure waste.
    """
    path = Path(directory) / SHA256SUMS_NAME
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # "<digest>  <name>" or "<digest> *<name>" (binary mode)
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*").strip()
        out[name] = digest.lower()
    return out


@lru_cache(maxsize=32)
def read_session_manifest(directory) -> dict:
    """Parse session_manifest.json. Empty dict if absent or unreadable."""
    path = Path(directory) / SESSION_MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def manifest_entry_for(directory, filename: str) -> dict:
    """The session_manifest captures[] entry for one dump, or {}."""
    manifest = read_session_manifest(directory)
    for entry in manifest.get("captures", []) or []:
        if entry.get("file") == filename:
            return entry
    return {}


def expected_digest_for(path, sidecar: dict | None = None) -> tuple:
    """Return (digest, where_it_came_from) for one capture file.

    Sidecar ``file_sha256`` wins over SHA256SUMS -- it is written by the same
    process that wrote the dump, in the same moment. Returns (None, None) when
    neither source has one, which is every capture taken before this was added.
    """
    path = Path(path)
    if sidecar:
        digest = sidecar.get("file_sha256")
        if digest:
            return str(digest).lower(), "sidecar"
    sums = read_sha256sums(path.parent)
    if path.name in sums:
        return sums[path.name], SHA256SUMS_NAME
    return None, None


@dataclass(frozen=True)
class ContentVerification:
    """The outcome of checking a capture's bytes against a recorded digest."""

    sha256: str = ""                      # computed from the bytes we actually read
    expected_sha256: str | None = None    # what the recorded source says it should be
    source: str | None = None             # "sidecar" | "SHA256SUMS" | None
    wire_sha256: str | None = None        # recorded hash of the .wire.bin, if any
    wire_file: str | None = None
    wire_bytes: int | None = None
    config_sha256: str | None = None

    @property
    def checked(self) -> bool:
        """True when there was a recorded digest to compare against."""
        return self.expected_sha256 is not None

    @property
    def verified(self) -> bool | None:
        """True / False when checked, None when nothing was recorded."""
        if not self.checked:
            return None
        return self.sha256 == self.expected_sha256

    def describe(self) -> str:
        if not self.checked:
            return "sha256 not recorded for this capture"
        if self.verified:
            return f"sha256 verified against {self.source} ({self.sha256[:12]}...)"
        return (f"SHA256 MISMATCH vs {self.source}: "
                f"computed {self.sha256[:12]}..., expected {self.expected_sha256[:12]}...")


def verify_bytes(raw: bytes, path=None, sidecar: dict | None = None) -> ContentVerification:
    """Hash the bytes we read and compare against whatever was recorded."""
    sidecar = sidecar or {}
    expected, source = (expected_digest_for(path, sidecar) if path is not None
                        else (sidecar.get("file_sha256"), "sidecar" if sidecar.get("file_sha256") else None))
    return ContentVerification(
        sha256=sha256_bytes(raw),
        expected_sha256=str(expected).lower() if expected else None,
        source=source,
        wire_sha256=sidecar.get("wire_sha256"),
        wire_file=sidecar.get("wire_file"),
        wire_bytes=sidecar.get("wire_bytes"),
        config_sha256=sidecar.get("config_sha256"),
    )


def verify_directory(directory, *, progress=None) -> dict:
    """Check every file listed in a directory's SHA256SUMS.

    Returns {"ok": [...], "mismatch": [...], "missing": [...], "unlisted": [...]}
    -- the same answer ``sha256sum -c`` gives, plus files present on disk that
    the sums file does not cover.
    """
    directory = Path(directory)
    sums = read_sha256sums(directory)
    out = {"ok": [], "mismatch": [], "missing": [], "unlisted": []}
    for name, expected in sorted(sums.items()):
        target = directory / name
        if not target.exists():
            out["missing"].append(name)
            continue
        actual = sha256_file(target)
        (out["ok"] if actual == expected else out["mismatch"]).append(name)
        if progress:
            progress(f"    {name}: {'ok' if actual == expected else 'MISMATCH'}")
    if sums:
        on_disk = {p.name for p in directory.glob("*.l3dump")}
        out["unlisted"] = sorted(on_disk - set(sums))
    return out
