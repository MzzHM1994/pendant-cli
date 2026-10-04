"""Dated-folder filing. No pendant, no model."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from pendant_client.archive import (
    INDEX_NAME,
    archive_sync_output,
    capture_sidecars,
    dated_dir,
    sha256_file,
)


def test_dated_dir_layout() -> None:
    library = Path("/library")
    assert dated_dir(library, datetime(2026, 1, 2, 15, 4)) == (
        Path("/library/2026/01/2026-01-02")
    )
    assert dated_dir(library, datetime(2026, 10, 4, 9, 0)) == (
        Path("/library/2026/10/2026-10-04")
    )


def test_sidecar_names_match_existing_sync() -> None:
    """Keep the archiver aligned with cli.py's sync filenames."""
    out = Path("today.opus")
    sidecars = capture_sidecars(out)
    assert sidecars["bin"] == Path("today.bin")
    assert sidecars["manifest"] == Path("today.opus.manifest.json")
    assert sidecars["events"] == Path("today.opus.events.json")
    assert sidecars["key"] == Path("today.opus.key.pem")

    cli = Path(__file__).resolve().parents[1] / "src" / "pendant_client" / "cli.py"
    source = cli.read_text(encoding="utf-8")
    assert 'out.suffix + ".manifest.json"' in source
    assert 'out.suffix + ".events.json"' in source
    assert 'audio_base.suffix + ".key.pem"' in source


def _write_sync(staging: Path, *, left: bytes = b"LEFT-AUDIO",
                right: bytes = b"RIGHT-AUDIO") -> Path:
    staging.mkdir(parents=True, exist_ok=True)
    capture = staging / "capture.opus"
    left_path = staging / "capture_rec0000.opus"
    right_path = staging / "capture_rec0001.opus"
    left_path.write_bytes(left)
    right_path.write_bytes(right)
    # mtime is deliberately not the recording date
    old = 1_600_000_000
    os.utime(left_path, (old, old))
    os.utime(right_path, (old, old))
    (staging / "capture.bin").write_bytes(b"RAWBIN")
    (staging / "capture.opus.events.json").write_text("{\"events\": []}\n",
                                                      encoding="utf-8")
    (staging / "capture.opus.key.pem").write_bytes(b"PRIVATE")
    manifest = {
        "sync": {
            "finished_at_iso": "2026-10-04T18:00:00.000",
            "address": "AA:BB:CC:DD:EE:FF",
        },
        "recordings": [
            {
                "file": "capture_rec0000.opus",
                "first_chunk": {
                    "absolute_timestamp_iso": "2026-10-04T09:15:00.000",
                },
            },
            {
                "file": "capture_rec0001.opus",
                "first_chunk": {
                    "absolute_timestamp_iso": "2026-09-30T23:40:00.000",
                },
            },
        ],
    }
    (staging / "capture.opus.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return capture


def test_files_each_recording_by_manifest_date(tmp_path: Path) -> None:
    library = tmp_path / "Pendant"
    capture = _write_sync(tmp_path / "staging")
    report = archive_sync_output(capture, library, now=datetime(2026, 10, 4))

    october = library / "2026" / "10" / "2026-10-04" / "capture_rec0000.opus"
    september = library / "2026" / "09" / "2026-09-30" / "capture_rec0001.opus"
    assert october.read_bytes() == b"LEFT-AUDIO"
    assert september.read_bytes() == b"RIGHT-AUDIO"
    # Raw capture sits with the earliest recording, not the sync day.
    raw_dir = library / "2026" / "09" / "2026-09-30"
    assert (raw_dir / "capture.bin").read_bytes() == b"RAWBIN"
    assert (raw_dir / "capture.opus.manifest.json").is_file()
    assert (raw_dir / "capture.opus.events.json").is_file()
    assert (raw_dir / "capture.opus.key.pem").read_bytes() == b"PRIVATE"
    assert [item.action for item in report.recordings()] == ["filed", "filed"]

    index = json.loads((library / INDEX_NAME).read_text(encoding="utf-8"))
    assert index["files"][sha256_file(october)]["path"].endswith(
        "2026/10/2026-10-04/capture_rec0000.opus")


def test_archive_is_idempotent(tmp_path: Path) -> None:
    library = tmp_path / "Pendant"
    first = _write_sync(tmp_path / "staging-1")
    archive_sync_output(first, library)
    october = library / "2026" / "10" / "2026-10-04" / "capture_rec0000.opus"
    before = october.read_bytes()

    second = _write_sync(tmp_path / "staging-2")
    report = archive_sync_output(second, library)
    assert october.read_bytes() == before
    assert [item.action for item in report.recordings()] == ["skipped", "skipped"]
    opus_files = list(library.rglob("*.opus"))
    assert len(opus_files) == 2


def test_different_bytes_do_not_overwrite(tmp_path: Path) -> None:
    library = tmp_path / "Pendant"
    archive_sync_output(_write_sync(tmp_path / "staging-1"), library)
    original = library / "2026" / "10" / "2026-10-04" / "capture_rec0000.opus"
    archive_sync_output(
        _write_sync(tmp_path / "staging-2", left=b"NEW-AUDIO", right=b"RIGHT-AUDIO"),
        library,
    )
    assert original.read_bytes() == b"LEFT-AUDIO"
    siblings = list((library / "2026" / "10" / "2026-10-04").glob("capture_rec0000*.opus"))
    assert len(siblings) == 2
    bodies = {path.read_bytes() for path in siblings}
    assert bodies == {b"LEFT-AUDIO", b"NEW-AUDIO"}
    # The unchanged September recording is not duplicated.
    september = list((library / "2026" / "09" / "2026-09-30").glob("*.opus"))
    assert len(september) == 1


def test_year_1970_timestamp_does_not_create_a_1970_folder(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    opus = staging / "capture_rec0000.opus"
    opus.write_bytes(b"AUDIO")
    manifest = {
        "sync": {"finished_at_iso": "2026-10-04T12:00:00.000"},
        "recordings": [{
            "file": opus.name,
            "first_chunk": {"absolute_timestamp_iso": "1970-01-01T00:00:00.000"},
        }],
    }
    capture = staging / "capture.opus"
    (staging / "capture.opus.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    library = tmp_path / "Pendant"
    archive_sync_output(capture, library, now=datetime(2026, 10, 4))
    assert (library / "2026" / "10" / "2026-10-04" / opus.name).is_file()
    assert not (library / "1970").exists()
