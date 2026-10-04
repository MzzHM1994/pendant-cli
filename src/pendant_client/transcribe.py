"""Local transcripts for pendant .opus recordings.

The audio never leaves the machine. Transcription shells out to
whisper.cpp (`whisper-cli`), which is the Homebrew build on a Mac.
On an M-series Mac that build runs on the Metal GPU, so a `small`
model keeps up with everyday conversation without a PyTorch or MLX
install. faster-whisper has no Metal backend (it would sit on the
CPU). mlx-whisper is fast on Apple Silicon too, but it is a larger,
Apple-only Python stack and a worse fit for a one-time setup.

This module does not download models and does not open a network
connection. Install whisper.cpp and a ggml model first; see
README-MACOS.md.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, Protocol


DEFAULT_MODEL = "small"

# Sizes whisper.cpp ships as ggml-*.bin. `.en` models are English-only.
MODEL_SIZES = (
    "tiny",
    "tiny.en",
    "base",
    "base.en",
    "small",
    "small.en",
    "medium",
    "medium.en",
    "large-v3",
    "large-v3-turbo",
)

SKIP_TRANSCRIPT = "already has a transcript"


class TranscribeConfigError(Exception):
    """whisper.cpp, ffmpeg, or the model file is not available locally."""


class TranscribeFailed(Exception):
    """ffmpeg or whisper.cpp failed on one recording."""


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    text: str
    segments: list[Segment]
    language: str
    model: str


class TranscriptionEngine(Protocol):
    model_name: str

    def transcribe(self, wav_path: Path) -> TranscriptResult:
        """Transcribe a 16 kHz mono WAV. No network."""


@dataclass
class TranscriptOutcome:
    source: Path
    action: str  # transcribed, skipped, failed
    detail: str
    outputs: tuple[Path, ...] = ()
    recorded_on: str = ""


@dataclass
class WhisperCppEngine:
    """whisper.cpp via the `whisper-cli` binary.

    Metal is on by default in the macOS Homebrew build. This class
    does not pass `--no-gpu`, so the GPU stays available.
    """

    model_path: Path
    binary: str
    ffmpeg: str
    language: str
    model_name: str

    def argv(self, wav_path: Path, out_base: Path) -> list[str]:
        return [
            self.binary,
            "-m", str(self.model_path),
            "-f", str(wav_path),
            "-l", self.language,
            "-of", str(out_base),
            "-otxt",
            "-osrt",
            "-oj",
            "-np",
        ]

    def transcribe(self, wav_path: Path) -> TranscriptResult:
        with tempfile.TemporaryDirectory(prefix="pendant-whisper-") as tmp:
            out_base = Path(tmp) / "out"
            argv = self.argv(wav_path, out_base)
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            json_path = Path(str(out_base) + ".json")
            if proc.returncode != 0 or not json_path.is_file():
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                detail = "\n".join(tail[-20:])
                raise TranscribeFailed(
                    "whisper.cpp could not transcribe "
                    f"{wav_path.name} (exit {proc.returncode}).\n{detail}".rstrip()
                )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        return parse_whisper_cpp_json(payload, model=self.model_name)


def model_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_dir = os.environ.get("PENDANT_MODEL_DIR")
    if env_dir:
        dirs.append(Path(env_dir).expanduser())
    home = Path.home()
    dirs.append(home / "Library" / "Application Support" / "pendant-cli" / "models")
    dirs.append(home / ".pendant-cli" / "models")
    dirs.append(Path.cwd() / "models")
    return dirs


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_whisper_cli(explicit: Optional[str] = None) -> str:
    """Locate the whisper.cpp CLI. Never downloads it."""
    if explicit:
        path = Path(explicit).expanduser()
        if _executable(path):
            return str(path)
        raise TranscribeConfigError(
            f"Could not find whisper.cpp at {explicit}.\n"
            "On a Mac, install it with:\n"
            "  brew install whisper.cpp\n"
            "The program you want is called whisper-cli."
        )

    candidates: list[str] = []
    env = os.environ.get("PENDANT_WHISPER_CLI")
    if env:
        candidates.append(env)
    for name in ("whisper-cli", "whisper-cpp"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend([
        "/opt/homebrew/bin/whisper-cli",
        "/usr/local/bin/whisper-cli",
        "/opt/homebrew/bin/whisper-cpp",
        "/usr/local/bin/whisper-cpp",
    ])
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate).expanduser()
        if _executable(path):
            return str(path)
    raise TranscribeConfigError(
        "Could not find whisper.cpp (the whisper-cli program).\n"
        "On an M-series Mac this is the fast local option: Homebrew's "
        "build uses the Metal GPU, and transcription stays on this Mac.\n"
        "Install it with:\n"
        "  brew install whisper.cpp\n"
        "Then download a model (one time) with:\n"
        "  bash scripts/download-whisper-model.sh small\n"
        "See README-MACOS.md."
    )


def find_ffmpeg(explicit: Optional[str] = None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if _executable(path):
            return str(path)
        raise TranscribeConfigError(
            f"Could not find ffmpeg at {explicit}.\n"
            "On a Mac, install it with:\n"
            "  brew install ffmpeg"
        )
    candidates: list[str] = []
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    candidates.extend([
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ])
    for candidate in candidates:
        path = Path(candidate)
        if _executable(path):
            return str(path)
    raise TranscribeConfigError(
        "Could not find ffmpeg, which converts each .opus recording "
        "into a WAV that whisper.cpp can read.\n"
        "On a Mac, install it with:\n"
        "  brew install ffmpeg"
    )


def resolve_model_path(model: str) -> Path:
    """Resolve a size name (`small`) or a path to a local ggml .bin file."""
    raw = Path(model).expanduser()
    looks_like_path = (
        raw.is_absolute()
        or model.startswith("~")
        or "/" in model
        or "\\" in model
    )
    if looks_like_path:
        if raw.is_file():
            return raw.resolve()
        raise TranscribeConfigError(
            f"Model file not found: {raw}\n"
            "Check the path, or download a model with:\n"
            "  bash scripts/download-whisper-model.sh small"
        )
    if raw.is_file():
        return raw.resolve()

    names = [model] if model.endswith(".bin") else [
        f"ggml-{model}.bin",
        f"{model}.bin",
    ]
    searched: list[Path] = []
    for directory in model_search_dirs():
        for name in names:
            path = directory / name
            searched.append(path)
            if path.is_file():
                return path.resolve()

    known = ", ".join(MODEL_SIZES)
    looked = "\n".join(f"  - {path}" for path in searched)
    size_hint = model if model in MODEL_SIZES or model.endswith(".bin") else "small"
    if size_hint.endswith(".bin"):
        size_hint = "small"
    raise TranscribeConfigError(
        f"Could not find a local whisper.cpp model for {model!r}.\n"
        "Download one once (this is the only network step):\n"
        f"  bash scripts/download-whisper-model.sh {size_hint}\n"
        f"Model sizes: {known}\n"
        "The default used by sync-and-transcribe is small.\n"
        "Looked in:\n"
        f"{looked}"
    )


def model_label(model: str, path: Path) -> str:
    if model.endswith(".bin") or "/" in model or model.startswith("~"):
        stem = path.stem
        if stem.startswith("ggml-"):
            return stem[len("ggml-"):]
        return stem
    return model


def build_whisper_cpp_engine(
    *,
    model: str = DEFAULT_MODEL,
    binary: Optional[str] = None,
    ffmpeg: Optional[str] = None,
    language: str = "en",
) -> WhisperCppEngine:
    """Fail early, before any pendant download, if local tools are missing."""
    binary_path = find_whisper_cli(binary)
    ffmpeg_path = find_ffmpeg(ffmpeg)
    model_path = resolve_model_path(model)
    return WhisperCppEngine(
        model_path=model_path,
        binary=binary_path,
        ffmpeg=ffmpeg_path,
        language=language or "en",
        model_name=model_label(model, model_path),
    )


def convert_opus_to_wav(src: Path, dst: Path, *, ffmpeg: str) -> None:
    """16 kHz mono PCM WAV, which is what whisper.cpp expects."""
    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(dst),
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not dst.is_file():
        detail = (proc.stderr or "").strip()
        raise TranscribeFailed(
            f"ffmpeg could not read {src.name}. "
            "The recording may be empty or damaged.\n"
            f"{detail}".rstrip()
        )


def _parse_timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        return float(text)
    except ValueError:
        return 0.0


def parse_whisper_cpp_json(payload: dict, *, model: str) -> TranscriptResult:
    """Read whisper.cpp's `-oj` JSON.

    Segment `offsets` are milliseconds (`t0 * 10` in whisper.cpp, and
    whisper's t0 is in centiseconds).
    """
    segments: list[Segment] = []
    items = payload.get("transcription") or payload.get("segments") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        offsets = item.get("offsets") or {}
        if isinstance(offsets, dict) and "from" in offsets and "to" in offsets:
            start = float(offsets["from"]) / 1000.0
            end = float(offsets["to"]) / 1000.0
        elif "start" in item and "end" in item:
            start = float(item["start"])
            end = float(item["end"])
        else:
            timestamps = item.get("timestamps") or {}
            if not isinstance(timestamps, dict):
                timestamps = {}
            start = _parse_timestamp(timestamps.get("from", 0))
            end = _parse_timestamp(timestamps.get("to", 0))
        if end < start:
            end = start
        segments.append(Segment(start=start, end=end, text=text))

    language = ""
    result = payload.get("result") or {}
    if isinstance(result, dict):
        language = str(result.get("language") or "")
    text = " ".join(segment.text for segment in segments if segment.text).strip()
    return TranscriptResult(
        text=text,
        segments=segments,
        language=language or "en",
        model=model,
    )


def format_srt_timestamp(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def render_srt(segments: list[Segment], fallback_text: str = "") -> str:
    cues = [segment for segment in segments if segment.text.strip()]
    if not cues and fallback_text.strip():
        cues = [Segment(0.0, 0.0, fallback_text.strip())]
    if not cues:
        cues = [Segment(0.0, 0.0, "(no speech detected)")]
    blocks: list[str] = []
    for index, segment in enumerate(cues, start=1):
        text = segment.text.replace("\r", " ").replace("\n", " ").strip()
        start = format_srt_timestamp(segment.start)
        end = format_srt_timestamp(segment.end)
        blocks.append(f"{index}\n{start} --> {end}\n{text}\n")
    return "\n".join(blocks)


def transcript_paths(opus: Path) -> dict[str, Path]:
    return {
        "txt": opus.with_suffix(".txt"),
        "srt": opus.with_suffix(".srt"),
        "json": opus.with_suffix(".json"),
    }


def has_transcript(opus: Path) -> bool:
    """A recording is done when the dated .txt, .srt, and .json all exist.

    Reruns skip these files. A partial set (only the .txt, for example)
    is transcribed again so the missing pieces get filled in.
    """
    for path in transcript_paths(opus).values():
        if not path.is_file() or path.stat().st_size == 0:
            return False
    return True


_DAY_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def recorded_on_for(opus: Path) -> date:
    """Prefer a YYYY-MM-DD folder name, then the file's modified date."""
    for parent in opus.parents:
        if _DAY_FOLDER.match(parent.name):
            return date.fromisoformat(parent.name)
    return datetime.fromtimestamp(opus.stat().st_mtime).date()


def write_transcript(
    opus: Path,
    result: TranscriptResult,
    recorded_on: date,
) -> tuple[Path, Path, Path]:
    paths = transcript_paths(opus)
    body = result.text.strip()
    if not body:
        body = "(no speech detected)"
    txt = (
        f"Date: {recorded_on.isoformat()}\n"
        f"Recording: {opus.name}\n"
        f"Model: {result.model}\n"
        f"Language: {result.language}\n"
        f"\n"
        f"{body}\n"
    )
    payload = {
        "date": recorded_on.isoformat(),
        "recording": opus.name,
        "model": result.model,
        "language": result.language,
        "text": body,
        "segments": [
            {
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": segment.text.strip(),
            }
            for segment in result.segments
        ],
    }
    srt = render_srt(result.segments, fallback_text=body)
    blobs = {
        paths["txt"]: txt,
        paths["srt"]: srt,
        paths["json"]: json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    }
    written: list[Path] = []
    try:
        for path, contents in blobs.items():
            path.write_text(contents, encoding="utf-8")
            written.append(path)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return paths["txt"], paths["srt"], paths["json"]


def display_path(path: Path, root: Optional[Path] = None) -> str:
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return str(path)


def format_outcome(outcome: TranscriptOutcome, root: Optional[Path] = None) -> str:
    label = display_path(outcome.source, root)
    if outcome.action == "skipped":
        return f"Skipped {label} ({outcome.detail})"
    if outcome.action == "failed":
        return f"Failed {label}: {outcome.detail}"
    names = ", ".join(path.name for path in outcome.outputs)
    return f"Transcribed {label} -> {names}"


def iter_opus_files(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() == ".opus":
            return [root]
        return []
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".opus":
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        found.append(path)
    return sorted(found)


def transcribe_one(
    opus: Path,
    *,
    engine: TranscriptionEngine,
    force: bool = False,
    on_event: Optional[Callable[[str], None]] = None,
    root: Optional[Path] = None,
) -> TranscriptOutcome:
    recorded = recorded_on_for(opus)
    if not force and has_transcript(opus):
        outcome = TranscriptOutcome(
            source=opus,
            action="skipped",
            detail=SKIP_TRANSCRIPT,
            outputs=tuple(transcript_paths(opus).values()),
            recorded_on=recorded.isoformat(),
        )
        if on_event:
            on_event(format_outcome(outcome, root))
        return outcome

    label = display_path(opus, root)
    if on_event:
        on_event(f"Transcribing {label} with model {engine.model_name}")

    ffmpeg = getattr(engine, "ffmpeg", None) or "ffmpeg"
    try:
        with tempfile.TemporaryDirectory(prefix="pendant-wav-") as tmp:
            wav = Path(tmp) / (opus.stem + ".wav")
            convert_opus_to_wav(opus, wav, ffmpeg=ffmpeg)
            result = engine.transcribe(wav)
        outputs = write_transcript(opus, result, recorded)
    except Exception as exc:  # noqa: BLE001 — one bad file must not stop the rest
        outcome = TranscriptOutcome(
            source=opus,
            action="failed",
            detail=str(exc),
            recorded_on=recorded.isoformat(),
        )
        if on_event:
            on_event(format_outcome(outcome, root))
        return outcome

    outcome = TranscriptOutcome(
        source=opus,
        action="transcribed",
        detail=result.model,
        outputs=outputs,
        recorded_on=recorded.isoformat(),
    )
    if on_event:
        on_event(format_outcome(outcome, root))
    return outcome


def transcribe_tree(
    root: Path,
    *,
    engine: TranscriptionEngine,
    force: bool = False,
    on_event: Optional[Callable[[str], None]] = None,
) -> list[TranscriptOutcome]:
    """Transcribe every .opus under `root`. Skip ones that already have a transcript."""
    outcomes: list[TranscriptOutcome] = []
    base = root if root.is_dir() else root.parent
    for opus in iter_opus_files(root):
        outcomes.append(transcribe_one(
            opus,
            engine=engine,
            force=force,
            on_event=on_event,
            root=base if root.is_dir() else None,
        ))
    return outcomes


def transcript_summary(outcomes: list[TranscriptOutcome]) -> str:
    transcribed = sum(1 for item in outcomes if item.action == "transcribed")
    skipped = sum(1 for item in outcomes if item.action == "skipped")
    failed = sum(1 for item in outcomes if item.action == "failed")
    return (
        f"Transcripts: transcribed {transcribed}, "
        f"skipped {skipped} already transcribed, "
        f"failed {failed}."
    )
