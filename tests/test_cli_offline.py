"""CLI wiring for transcribe and sync-and-transcribe.

These invoke Click in-process. They do not open Bluetooth and they do
not load a whisper model.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from pendant_client.cli import (
    main,
    run_device_sync,
    sync,
    sync_and_transcribe_cmd,
    transcribe_cmd,
)
from pendant_client.transcribe import TranscribeConfigError, TranscriptResult, Segment


class FakeEngine:
    def __init__(self):
        self.model_name = "tiny"
        self.ffmpeg = "ffmpeg"
        self.calls: list[str] = []

    def transcribe(self, wav_path: Path) -> TranscriptResult:
        self.calls.append(wav_path.name)
        return TranscriptResult(
            text="wired through the cli",
            segments=[Segment(0.0, 1.0, "wired through the cli")],
            language="en",
            model="tiny",
        )


def _combined(result) -> str:
    stderr = getattr(result, "stderr", None) or ""
    return (result.output or "") + stderr


def _stub_ffmpeg(monkeypatch) -> None:
    def convert(src: Path, dst: Path, *, ffmpeg: str) -> None:
        dst.write_bytes(b"RIFF")

    monkeypatch.setattr(
        "pendant_client.transcribe.convert_opus_to_wav", convert)


def test_help_lists_the_new_commands() -> None:
    runner = CliRunner()
    top = runner.invoke(main, ["--help"])
    assert top.exit_code == 0
    assert "sync-and-transcribe" in top.output
    assert "transcribe" in top.output

    sync_help = runner.invoke(main, ["sync-and-transcribe", "--help"])
    text = _combined(sync_help)
    assert sync_help.exit_code == 0
    assert "--model" in text
    assert "--library" in text
    assert "unpair" in text
    assert "clear-storage" in text
    assert "factory-reset" in text

    transcribe_help = runner.invoke(main, ["transcribe", "--help"])
    assert transcribe_help.exit_code == 0
    assert "--model" in transcribe_help.output
    assert "--force" in transcribe_help.output


def test_transcribe_command_uses_the_engine(tmp_path: Path, monkeypatch) -> None:
    opus = tmp_path / "note.opus"
    opus.write_bytes(b"opus-bytes")
    engine = FakeEngine()
    monkeypatch.setattr(
        "pendant_client.cli.build_whisper_cpp_engine",
        lambda **kwargs: engine,
    )
    _stub_ffmpeg(monkeypatch)

    result = CliRunner().invoke(
        main, ["transcribe", str(tmp_path), "--model", "tiny"])
    assert result.exit_code == 0, _combined(result)
    assert engine.calls
    body = (tmp_path / "note.txt").read_text(encoding="utf-8")
    assert body.startswith("Date: ")
    assert "wired through the cli" in body
    assert (tmp_path / "note.srt").is_file()
    assert (tmp_path / "note.json").is_file()
    assert "Transcripts: transcribed 1" in result.output

    again = CliRunner().invoke(
        main, ["transcribe", str(tmp_path), "--model", "tiny"])
    assert again.exit_code == 0, _combined(again)
    assert len(engine.calls) == 1
    assert "already has a transcript" in again.output


def test_sync_and_transcribe_command(tmp_path: Path, monkeypatch) -> None:
    engine = FakeEngine()
    seen: dict = {}

    def build(**kwargs):
        seen["model"] = kwargs["model"]
        return engine

    def sync_fn(**kwargs):
        seen["sync"] = kwargs
        base = Path(kwargs["out_path"])
        opus = base.parent / "capture_rec0000.opus"
        opus.write_bytes(b"FROM-PENDANT")
        manifest = {
            "sync": {"finished_at_iso": "2026-05-02T08:00:00.000"},
            "recordings": [{
                "file": opus.name,
                "first_chunk": {
                    "absolute_timestamp_iso": "2026-05-02T07:30:00.000",
                },
            }],
        }
        base.with_suffix(base.suffix + ".manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        (base.parent / "capture.bin").write_bytes(b"BIN")

    monkeypatch.setattr("pendant_client.cli.build_whisper_cpp_engine", build)
    monkeypatch.setattr("pendant_client.cli.run_device_sync", sync_fn)
    _stub_ffmpeg(monkeypatch)

    library = tmp_path / "library"
    result = CliRunner().invoke(main, [
        "sync-and-transcribe", "DE:AD:BE:EF:00:00",
        "--library", str(library),
        "--model", "tiny",
    ])
    assert result.exit_code == 0, _combined(result)
    assert seen["model"] == "tiny"
    assert seen["sync"]["address"] == "DE:AD:BE:EF:00:00"
    assert "upload_url" not in seen["sync"]
    filed = library / "2026" / "05" / "2026-05-02" / "capture_rec0000.opus"
    assert filed.read_bytes() == b"FROM-PENDANT"
    transcript = filed.with_suffix(".txt").read_text(encoding="utf-8")
    assert transcript.startswith("Date: 2026-05-02\n")
    assert "Ready: whisper.cpp model tiny" in result.output
    assert "Done. Filed 1 recording(s)" in result.output

    second = CliRunner().invoke(main, [
        "sync-and-transcribe", "DE:AD:BE:EF:00:00",
        "--library", str(library),
        "--model", "tiny",
    ])
    assert second.exit_code == 0, _combined(second)
    assert len(engine.calls) == 1
    assert "skipped 1 already archived" in second.output
    assert "skipped 1 already transcribed" in second.output


def test_does_not_download_when_whisper_is_missing(tmp_path: Path, monkeypatch) -> None:
    called = []

    def boom(**kwargs):
        raise TranscribeConfigError("Could not find whisper.cpp")

    monkeypatch.setattr("pendant_client.cli.build_whisper_cpp_engine", boom)
    monkeypatch.setattr(
        "pendant_client.cli.run_device_sync",
        lambda **kwargs: called.append(kwargs),
    )
    result = CliRunner().invoke(main, [
        "sync-and-transcribe", "AA:BB:CC:DD:EE:FF",
        "--library", str(tmp_path),
    ])
    assert result.exit_code != 0
    assert called == []
    assert "whisper.cpp" in _combined(result)


def test_run_device_sync_is_offline_and_decodes(monkeypatch) -> None:
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(sync, "callback", fake)
    run_device_sync(
        address="AA:BB:CC:DD:EE:FF",
        out_path="today.opus",
        key_path=None,
        push_key=False,
        idle=5.0,
        include_logs=False,
    )
    assert captured == {
        "address": "AA:BB:CC:DD:EE:FF",
        "out_path": "today.opus",
        "key_path": None,
        "push_key": False,
        "idle": 5.0,
        "no_decode": False,
        "include_logs": False,
        "upload_url": None,
    }


def test_run_device_sync_reports_a_failed_download(monkeypatch) -> None:
    def fake(**kwargs):
        raise SystemExit(2)

    monkeypatch.setattr(sync, "callback", fake)
    with pytest.raises(click.ClickException):
        run_device_sync(
            address="AA:BB",
            out_path="today.opus",
            key_path=None,
            push_key=False,
            idle=5.0,
            include_logs=False,
        )


def test_new_commands_do_not_call_destructive_pendant_ops() -> None:
    source = "\n".join([
        inspect.getsource(run_device_sync),
        inspect.getsource(sync_and_transcribe_cmd.callback),
        inspect.getsource(transcribe_cmd.callback),
    ])
    for banned in (
        "unpair(",
        "clear_storage",
        "clear-storage(",
        "factory_reset",
        "reset_device",
        "ClearPendantStorage",
        "FactoryResetPendant",
        "UnpairBluetooth",
    ):
        assert banned not in source
    assert "upload_url=None" in inspect.getsource(run_device_sync)


def test_packaging_and_scripts_point_at_the_safe_command() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pendant-cli = "pendant_client.cli:main"' in pyproject
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    script = (root / "scripts" / "sync-and-transcribe.sh").read_text(encoding="utf-8")
    readme = (root / "README-MACOS.md").read_text(encoding="utf-8")
    assert "pendant-cli sync-and-transcribe" in makefile
    assert "pendant sync-and-transcribe" in script
    assert "pendant scan" in readme
    assert "Never run" in readme
    for blob in (makefile, script):
        lowered = blob.lower()
        assert "unpair" not in lowered
        assert "clear-storage" not in lowered
        assert "factory-reset" not in lowered
