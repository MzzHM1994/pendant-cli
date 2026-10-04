"""File a pendant sync into dated folders.

Layout, under the library root:

    YYYY/MM/YYYY-MM-DD/<recording>.opus
    YYYY/MM/YYYY-MM-DD/<recording>.txt
    YYYY/MM/YYYY-MM-DD/<recording>.srt
    YYYY/MM/YYYY-MM-DD/<recording>.json

The date comes from the sync manifest (`first_chunk.absolute_timestamp_iso`),
which the existing `pendant sync` command already writes. The raw `.bin`,
manifest, events, and key (if one was saved) go in the folder of the
earliest recording.

Filing is idempotent: a file whose bytes are already at the destination
is left alone. A different recording that happens to use the same name
is stored beside it with a short hash, so nothing is overwritten.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


INDEX_NAME = "archive-index.json"
SKIP_ARCHIVE = "already archived"


@dataclass
class FilePlacement:
    source_name: str
    dest: Optional[Path]
    action: str  # filed, skipped, missing
    sha256: str
    kind: str  # recording, raw


@dataclass
class ArchiveReport:
    placements: list[FilePlacement]

    def recordings(self, action: Optional[str] = None) -> list[FilePlacement]:
        items = [item for item in self.placements if item.kind == "recording"]
        if action is None:
            return items
        return [item for item in items if item.action == action]


def dated_dir(library: Path, when: datetime) -> Path:
    day = when.date()
    return library / f"{day.year:04d}" / f"{day.month:02d}" / day.isoformat()


def capture_sidecars(capture_base: Path) -> dict[str, Path]:
    """Match the filenames `pendant sync -o <capture_base>` already writes.

    See `cli.py`: `_resolve_paths`, `_write_sync_manifest`, and the
    `--push-key` private-key path. Those writers are not modified here.
    """
    if capture_base.suffix:
        return {
            "manifest": capture_base.with_suffix(
                capture_base.suffix + ".manifest.json"),
            "events": capture_base.with_suffix(
                capture_base.suffix + ".events.json"),
            "key": capture_base.with_suffix(capture_base.suffix + ".key.pem"),
            "bin": capture_base.with_suffix(".bin"),
        }
    return {
        "manifest": capture_base.with_name(capture_base.name + ".manifest.json"),
        "events": capture_base.with_name(capture_base.name + ".events.json"),
        "key": capture_base.with_name(capture_base.name + ".key.pem"),
        "bin": capture_base.with_name(capture_base.name + ".bin"),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_when(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.year < 2000:
        return None
    return parsed


def recording_datetime(recording: dict, fallback: datetime) -> datetime:
    first = recording.get("first_chunk") or {}
    if isinstance(first, dict):
        parsed = _parse_when(first.get("absolute_timestamp_iso"))
        if parsed is not None:
            return parsed
    return fallback


def load_index(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "files": {}}
    if not isinstance(data, dict):
        return {"version": 1, "files": {}}
    files = data.get("files")
    if not isinstance(files, dict):
        data["files"] = {}
    data["version"] = 1
    return data


def save_index(library: Path, placements: list[FilePlacement]) -> None:
    path = library / INDEX_NAME
    index = load_index(path)
    files = index["files"]
    for item in placements:
        if item.dest is None or not item.sha256:
            continue
        files[item.sha256] = {
            "path": item.dest.relative_to(library).as_posix(),
            "sha256": item.sha256,
            "kind": item.kind,
        }
    index["files"] = files
    payload = json.dumps(index, indent=2) + "\n"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def place_file(src: Path, dest_dir: Path, *, kind: str) -> FilePlacement:
    if not src.is_file():
        return FilePlacement(
            source_name=src.name, dest=None, action="missing", sha256="", kind=kind,
        )
    digest = sha256_file(src)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists() and sha256_file(dest) == digest:
        return FilePlacement(
            source_name=src.name, dest=dest, action="skipped", sha256=digest, kind=kind,
        )
    if dest.exists():
        dest = dest_dir / f"{src.stem}-{digest[:8]}{src.suffix}"
        if dest.exists() and sha256_file(dest) == digest:
            return FilePlacement(
                source_name=src.name, dest=dest, action="skipped",
                sha256=digest, kind=kind,
            )
    shutil.copy2(src, dest)
    return FilePlacement(
        source_name=src.name, dest=dest, action="filed", sha256=digest, kind=kind,
    )


def _read_manifest(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def archive_sync_output(
    capture_base: Path,
    library: Path,
    *,
    now: Optional[datetime] = None,
) -> ArchiveReport:
    """Copy sync products into dated folders. Does not delete sources.

    The caller owns the staging directory and removes it after a clean
    archive. Re-running with the same bytes does not duplicate files.
    """
    library.mkdir(parents=True, exist_ok=True)
    fallback = now or datetime.now()
    sidecars = capture_sidecars(capture_base)
    manifest = _read_manifest(sidecars["manifest"])
    finished = _parse_when((manifest.get("sync") or {}).get("finished_at_iso"))
    if finished is not None:
        fallback = finished

    placements: list[FilePlacement] = []
    dates: list[datetime] = []
    seen: set[Path] = set()
    parent = capture_base.parent

    def _mark(path: Path) -> None:
        try:
            seen.add(path.resolve())
        except OSError:
            seen.add(path)

    def _marked(path: Path) -> bool:
        try:
            return path.resolve() in seen
        except OSError:
            return path in seen

    for recording in manifest.get("recordings") or []:
        if not isinstance(recording, dict):
            continue
        name = recording.get("file")
        if not isinstance(name, str) or not name:
            continue
        src = parent / name
        _mark(src)
        when = recording_datetime(recording, fallback)
        dates.append(when)
        placements.append(place_file(src, dated_dir(library, when), kind="recording"))

    for src in sorted(parent.glob("*.opus")):
        if _marked(src) or src.name == capture_base.name:
            continue
        when = datetime.fromtimestamp(src.stat().st_mtime)
        if when.year < 2000:
            when = fallback
        dates.append(when)
        placements.append(place_file(src, dated_dir(library, when), kind="recording"))

    raw_when = min(dates) if dates else fallback
    raw_dir = dated_dir(library, raw_when)
    for key in ("bin", "manifest", "events", "key"):
        src = sidecars[key]
        if src.is_file():
            placements.append(place_file(src, raw_dir, kind="raw"))

    save_index(library, placements)
    return ArchiveReport(placements=placements)


def format_placement(item: FilePlacement, library: Path) -> str:
    if item.action == "missing":
        return f"Missing {item.source_name}"
    if item.dest is None:
        return f"Missing {item.source_name}"
    try:
        label = item.dest.relative_to(library).as_posix()
    except ValueError:
        label = str(item.dest)
    if item.action == "skipped":
        return f"Skipped {label} ({SKIP_ARCHIVE})"
    return f"Filed {label}"
