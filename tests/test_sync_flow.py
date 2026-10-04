"""sync-and-transcribe orchestration. Sync and whisper are both fakes."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from pendant_client.sync_flow import (
    SyncFailed,
    format_report,
    run_sync_and_transcribe,
)
from pendant_client.transcribe import (
    Segment,
    TranscriptResult,
    TranscribeFailed,
)


class FakeEngine:
    def __init__(self):
        self.model_name = "small"
        self.ffmpeg = "ffmpeg"
        self.calls: list[str] = []

    def transcribe(self, wav_path: Path) -> TranscriptResult:
        self.calls.append(wav_path.stem)
        return TranscriptResult(
            text="hello from the pendant",
            segments=[Segment(0.0, 1.25, "hello from the pendant")],
            language="en",
            model="small",
        )


def _stub_ffmpeg(monkeypatch) -> None:
    def convert(src: Path, dst: Path, *, ffmpeg: str) -> None:
        dst.write_bytes(b"RIFF")

    monkeypatch.setattr(
        "pendant_client.transcribe.convert_opus_to_wav", convert)


def _fake_sync_factory(blobs: dict[str, bytes]):
    calls: list[dict] = []

    def sync_fn(**kwargs):
        calls.append(kwargs)
        assert "upload_url" not in kwargs
        assert "no_decode" not in kwargs
        base = Path(kwargs["out_path"])
        base.parent.mkdir(parents=True, exist_ok=True)
        for name, body in blobs.items():
            (base.parent / name).write_bytes(body)
        manifest = {
            "sync": {
                "finished_at_iso": "2026-10-04T12:00:00.000",
                "address": kwargs["address"],
            },
            "recordings": [
                {
                    "file": name,
                    "first_chunk": {
                        "absolute_timestamp_iso": "2026-10-04T09:00:00.000",
                    },
                }
                for name in blobs
                if name.endswith(".opus") and name != "capture.opus"
            ],
        }
        manifest_path = base.with_suffix(base.suffix + ".manifest.json")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        events = base.with_suffix(base.suffix + ".events.json")
        events.write_text('{"events": []}\n', encoding="utf-8")
        (base.parent / "capture.bin").write_bytes(b"BIN")

    return sync_fn, calls


def test_flow_files_then_transcribes_and_is_idempotent(
    tmp_path: Path, monkeypatch,
) -> None:
    _stub_ffmpeg(monkeypatch)
    library = tmp_path / "Pendant"
    blobs = {"capture_rec0000.opus": b"AUDIO-A", "capture_rec0001.opus": b"AUDIO-B"}
    sync_fn, calls = _fake_sync_factory(blobs)
    engine = FakeEngine()
    lines: list[str] = []

    report = run_sync_and_transcribe(
        address="AA:BB:CC:DD:EE:FF",
        library=library,
        sync_fn=sync_fn,
        engine=engine,
        now=datetime(2026, 10, 4, 12, 0, 0),
        on_event=lines.append,
    )

    assert calls[0]["address"] == "AA:BB:CC:DD:EE:FF"
    assert calls[0]["key_path"] is None
    assert calls[0]["push_key"] is False
    assert calls[0]["include_logs"] is False
    day = library / "2026" / "10" / "2026-10-04"
    assert (day / "capture_rec0000.opus").read_bytes() == b"AUDIO-A"
    assert (day / "capture_rec0001.txt").read_text(encoding="utf-8").startswith(
        "Date: 2026-10-04\n")
    assert "00:00:00,000 --> 00:00:01,250" in (
        day / "capture_rec0000.srt").read_text(encoding="utf-8")
    assert json.loads((day / "capture_rec0001.json").read_text(encoding="utf-8"))[
        "text"] == "hello from the pendant"
    assert (day / "capture.bin").read_bytes() == b"BIN"
    assert not (library / ".staging").exists()
    assert sorted(engine.calls) == ["capture_rec0000", "capture_rec0001"]
    assert "Filed 2026/10/2026-10-04/capture_rec0000.opus" in lines
    assert any(line.startswith("Transcribing ") for line in lines)
    summary = format_report(report)
    assert summary == (
        "Done. Filed 2 recording(s), skipped 0 already archived. "
        "Transcripts: transcribed 2, skipped 0 already transcribed, failed 0."
    )

    again = run_sync_and_transcribe(
        address="AA:BB:CC:DD:EE:FF",
        library=library,
        sync_fn=sync_fn,
        engine=engine,
        now=datetime(2026, 10, 4, 13, 0, 0),
    )
    assert sorted(engine.calls) == ["capture_rec0000", "capture_rec0001"]
    assert format_report(again) == (
        "Done. Filed 0 recording(s), skipped 2 already archived. "
        "Transcripts: transcribed 0, skipped 2 already transcribed, failed 0."
    )
    assert len(list(library.rglob("*.opus"))) == 2


def test_does_not_archive_when_download_fails(tmp_path: Path) -> None:
    engine = FakeEngine()

    def sync_fn(**kwargs):
        raise RuntimeError("no pendant in this test")

    with pytest.raises(SyncFailed) as caught:
        run_sync_and_transcribe(
            address="AA:BB:CC:DD:EE:FF",
            library=tmp_path / "Pendant",
            sync_fn=sync_fn,
            engine=engine,
        )
    assert "no pendant" in str(caught.value)
    assert caught.value.staging.exists()
    assert engine.calls == []
    assert list((tmp_path / "Pendant").rglob("*.opus")) == []


def test_missing_recording_is_a_failure_and_keeps_staging(tmp_path: Path) -> None:
    engine = FakeEngine()

    def sync_fn(**kwargs):
        base = Path(kwargs["out_path"])
        manifest = {
            "recordings": [{
                "file": "capture_rec0000.opus",
                "first_chunk": {
                    "absolute_timestamp_iso": "2026-10-04T09:00:00.000",
                },
            }],
        }
        path = base.with_suffix(base.suffix + ".manifest.json")
        path.write_text(json.dumps(manifest), encoding="utf-8")

    report = run_sync_and_transcribe(
        address="AA:BB:CC:DD:EE:FF",
        library=tmp_path / "Pendant",
        sync_fn=sync_fn,
        engine=engine,
    )
    assert report.failed
    assert (tmp_path / "Pendant" / ".staging").is_dir()
    assert engine.calls == []


def test_one_transcript_failure_still_files_audio(
    tmp_path: Path, monkeypatch,
) -> None:
    _stub_ffmpeg(monkeypatch)

    class Flaky(FakeEngine):
        def transcribe(self, wav_path: Path) -> TranscriptResult:
            if wav_path.stem.endswith("0001"):
                raise TranscribeFailed("model exploded")
            return super().transcribe(wav_path)

    library = tmp_path / "Pendant"
    sync_fn, _calls = _fake_sync_factory({
        "capture_rec0000.opus": b"A",
        "capture_rec0001.opus": b"B",
    })
    report = run_sync_and_transcribe(
        address="AA:BB:CC:DD:EE:FF",
        library=library,
        sync_fn=sync_fn,
        engine=Flaky(),
    )
    day = library / "2026" / "10" / "2026-10-04"
    assert (day / "capture_rec0000.txt").is_file()
    assert (day / "capture_rec0001.opus").read_bytes() == b"B"
    assert not (day / "capture_rec0001.txt").exists()
    assert report.failed
