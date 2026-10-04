"""Local transcription. The engine is mocked; no model and no Bluetooth."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pendant_client.transcribe import (
    Segment,
    TranscribeConfigError,
    TranscribeFailed,
    TranscriptResult,
    WhisperCppEngine,
    build_whisper_cpp_engine,
    convert_opus_to_wav,
    find_ffmpeg,
    find_whisper_cli,
    format_srt_timestamp,
    has_transcript,
    iter_opus_files,
    parse_whisper_cpp_json,
    render_srt,
    resolve_model_path,
    transcribe_one,
    transcribe_tree,
    transcript_paths,
)


class FakeEngine:
    def __init__(self, model_name: str = "small"):
        self.model_name = model_name
        self.ffmpeg = "ffmpeg"
        self.calls: list[Path] = []

    def transcribe(self, wav_path: Path) -> TranscriptResult:
        self.calls.append(wav_path)
        return TranscriptResult(
            text="hello from the pendant",
            segments=[Segment(0.0, 1.5, "hello from the pendant")],
            language="en",
            model=self.model_name,
        )


def _stub_ffmpeg(monkeypatch) -> None:
    def convert(src: Path, dst: Path, *, ffmpeg: str) -> None:
        dst.write_bytes(b"RIFF")

    monkeypatch.setattr(
        "pendant_client.transcribe.convert_opus_to_wav", convert)


def test_format_srt_timestamp() -> None:
    assert format_srt_timestamp(1.5) == "00:00:01,500"
    assert format_srt_timestamp(3661.25) == "01:01:01,250"
    assert format_srt_timestamp(-1) == "00:00:00,000"


def test_parse_whisper_cpp_json_uses_millisecond_offsets() -> None:
    payload = {
        "result": {"language": "en"},
        "transcription": [
            {
                "timestamps": {"from": "00:00:00,000", "to": "00:00:01,500"},
                "offsets": {"from": 0, "to": 1500},
                "text": " hello from the pendant",
            },
            {
                "timestamps": {"from": "00:00:01,500", "to": "00:00:02,000"},
                "offsets": {"from": 1500, "to": 2000},
                "text": " okay",
            },
        ],
    }
    result = parse_whisper_cpp_json(payload, model="small")
    assert result.language == "en"
    assert result.model == "small"
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 1.5
    assert result.segments[0].text == "hello from the pendant"
    assert result.text == "hello from the pendant okay"


def test_parse_whisper_cpp_json_falls_back_to_timestamps() -> None:
    payload = {
        "transcription": [
            {"timestamps": {"from": "00:01:02,250", "to": "00:01:03,000"},
             "text": "later"},
        ],
    }
    result = parse_whisper_cpp_json(payload, model="base")
    assert result.segments[0].start == pytest.approx(62.25)
    assert result.segments[0].end == pytest.approx(63.0)


def test_render_srt_is_timestamped() -> None:
    text = render_srt([Segment(0.0, 1.5, "hello from the pendant")])
    assert text.startswith("1\n00:00:00,000 --> 00:00:01,500\n")
    assert "hello from the pendant" in text


def test_has_transcript_requires_txt_srt_and_json(tmp_path: Path) -> None:
    opus = tmp_path / "clip.opus"
    opus.write_bytes(b"opus")
    assert has_transcript(opus) is False
    paths = transcript_paths(opus)
    paths["txt"].write_text("Date: 2026-10-04\n\nhello\n", encoding="utf-8")
    paths["srt"].write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n",
                            encoding="utf-8")
    assert has_transcript(opus) is False
    paths["json"].write_text("{}\n", encoding="utf-8")
    assert has_transcript(opus) is True


def test_transcribe_writes_dated_sidecars_and_skips_on_rerun(
    tmp_path: Path, monkeypatch,
) -> None:
    day = tmp_path / "2026" / "10" / "2026-10-04"
    day.mkdir(parents=True)
    opus = day / "capture_rec0000.opus"
    opus.write_bytes(b"not-really-opus")
    # mtime must not win over the dated folder
    old = 1_577_836_800  # 2020-01-01 UTC-ish, local-dependent; folder wins
    import os
    os.utime(opus, (old, old))

    _stub_ffmpeg(monkeypatch)
    engine = FakeEngine(model_name="small")
    first = transcribe_one(opus, engine=engine, root=tmp_path)
    assert first.action == "transcribed"
    assert first.recorded_on == "2026-10-04"
    assert len(engine.calls) == 1

    txt = (day / "capture_rec0000.txt").read_text(encoding="utf-8")
    assert txt.startswith("Date: 2026-10-04\n")
    assert "Recording: capture_rec0000.opus\n" in txt
    assert "Model: small\n" in txt
    assert "hello from the pendant" in txt

    srt = (day / "capture_rec0000.srt").read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,500" in srt

    payload = json.loads((day / "capture_rec0000.json").read_text(encoding="utf-8"))
    assert payload["date"] == "2026-10-04"
    assert payload["model"] == "small"
    assert payload["segments"] == [
        {"start": 0.0, "end": 1.5, "text": "hello from the pendant"},
    ]

    second = transcribe_tree(tmp_path, engine=engine)
    assert [item.action for item in second] == ["skipped"]
    assert second[0].detail == "already has a transcript"
    assert len(engine.calls) == 1

    forced = transcribe_one(opus, engine=engine, force=True)
    assert forced.action == "transcribed"
    assert len(engine.calls) == 2


def test_transcribe_continues_after_one_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    _stub_ffmpeg(monkeypatch)
    good = tmp_path / "good.opus"
    bad = tmp_path / "bad.opus"
    later = tmp_path / "later.opus"
    for path in (good, bad, later):
        path.write_bytes(b"opus")

    class Flaky(FakeEngine):
        def transcribe(self, wav_path: Path) -> TranscriptResult:
            if wav_path.stem == "bad":
                raise TranscribeFailed("whisper.cpp could not transcribe bad.wav")
            return super().transcribe(wav_path)

    engine = Flaky()
    outcomes = transcribe_tree(tmp_path, engine=engine)
    by_name = {item.source.name: item.action for item in outcomes}
    assert by_name == {
        "good.opus": "transcribed",
        "bad.opus": "failed",
        "later.opus": "transcribed",
    }
    assert (tmp_path / "good.txt").is_file()
    assert not (tmp_path / "bad.txt").exists()
    assert (tmp_path / "later.txt").is_file()
    assert "whisper.cpp" in next(
        item.detail for item in outcomes if item.action == "failed")


def test_iter_opus_skips_hidden_staging(tmp_path: Path) -> None:
    keep = tmp_path / "2026" / "10" / "2026-10-04"
    keep.mkdir(parents=True)
    (keep / "keep.opus").write_bytes(b"a")
    staging = tmp_path / ".staging" / "run"
    staging.mkdir(parents=True)
    (staging / "secret.opus").write_bytes(b"b")
    found = [path.name for path in iter_opus_files(tmp_path)]
    assert found == ["keep.opus"]


def test_missing_whisper_binary_explains_brew() -> None:
    with pytest.raises(TranscribeConfigError) as caught:
        find_whisper_cli("/nonexistent/whisper-cli")
    message = str(caught.value)
    assert "brew install whisper.cpp" in message
    assert "whisper-cli" in message


def test_missing_ffmpeg_explains_brew() -> None:
    with pytest.raises(TranscribeConfigError) as caught:
        find_ffmpeg("/nonexistent/ffmpeg")
    assert "brew install ffmpeg" in str(caught.value)


def test_resolve_model_path_from_model_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PENDANT_MODEL_DIR", str(tmp_path))
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"ggml")
    found = resolve_model_path("small")
    assert found == model.resolve()
    explicit = resolve_model_path(str(model))
    assert explicit == model.resolve()


def test_resolve_model_path_missing_is_local_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PENDANT_MODEL_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TranscribeConfigError) as caught:
        resolve_model_path("small")
    message = str(caught.value)
    assert "download-whisper-model.sh small" in message
    assert "http://" not in message
    assert "https://" not in message


def test_whisper_cpp_argv_keeps_metal_and_parses_json(
    tmp_path: Path, monkeypatch,
) -> None:
    engine = WhisperCppEngine(
        model_path=tmp_path / "ggml-base.bin",
        binary="whisper-cli",
        ffmpeg="ffmpeg",
        language="en",
        model_name="base",
    )
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        out_base = argv[argv.index("-of") + 1]
        Path(out_base + ".json").write_text(json.dumps({
            "result": {"language": "en"},
            "transcription": [{
                "offsets": {"from": 0, "to": 2000},
                "text": " hi",
            }],
        }), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "pendant_client.transcribe.subprocess.run", fake_run)
    result = engine.transcribe(wav)
    argv = captured["argv"]
    assert argv[:6] == [
        "whisper-cli", "-m", str(tmp_path / "ggml-base.bin"),
        "-f", str(wav), "-l",
    ]
    assert "-otxt" in argv and "-osrt" in argv and "-oj" in argv
    assert "--no-gpu" not in argv and "-ng" not in argv
    assert result.text == "hi"
    assert result.model == "base"
    assert result.segments[0].end == 2.0


def test_whisper_cpp_failure_includes_stderr(tmp_path: Path, monkeypatch) -> None:
    engine = WhisperCppEngine(
        model_path=tmp_path / "ggml-small.bin",
        binary="whisper-cli",
        ffmpeg="ffmpeg",
        language="en",
        model_name="small",
    )

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="model file not found")

    monkeypatch.setattr(
        "pendant_client.transcribe.subprocess.run", fake_run)
    with pytest.raises(TranscribeFailed) as caught:
        engine.transcribe(tmp_path / "clip.wav")
    assert "model file not found" in str(caught.value)


def test_convert_opus_to_wav_argv(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "a.opus"
    dst = tmp_path / "a.wav"
    src.write_bytes(b"opus")
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        dst.write_bytes(b"RIFF")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "pendant_client.transcribe.subprocess.run", fake_run)
    convert_opus_to_wav(src, dst, ffmpeg="/opt/homebrew/bin/ffmpeg")
    assert seen["argv"][:3] == ["/opt/homebrew/bin/ffmpeg", "-hide_banner", "-loglevel"]
    assert seen["argv"][-1] == str(dst)
    assert "16000" in seen["argv"]
    assert "pcm_s16le" in seen["argv"]


def test_build_engine_requires_local_files(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "whisper-cli"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("#!/bin/sh\n", encoding="utf-8")
    ffmpeg.chmod(0o755)
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"m")
    monkeypatch.setenv("PENDANT_MODEL_DIR", str(tmp_path))
    engine = build_whisper_cpp_engine(
        model="tiny", binary=str(binary), ffmpeg=str(ffmpeg), language="en",
    )
    assert engine.model_name == "tiny"
    assert engine.binary == str(binary)
    assert engine.model_path == model.resolve()


def test_transcription_modules_do_not_use_the_network() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "pendant_client"
    for name in ("transcribe.py", "archive.py", "sync_flow.py"):
        text = (root / name).read_text(encoding="utf-8")
        for banned in (
            "httpx", "urllib", "requests", "socket", "openai",
            "ble.py", "crypto", "flash_page", "from .decode", "from .ble",
            "from .session", "unpair", "clear_storage", "factory_reset",
            "reset_device", "clear-storage", "factory-reset",
        ):
            assert banned not in text, f"{name} mentions {banned}"
