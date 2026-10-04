"""One-command download, file, and local transcribe.

Wraps the existing `pendant sync` command. This module does not open
the radio itself. Device maintenance commands stay on their original
CLI entries and are not parameters here.

The download step is injected so tests can run without a pendant.
`upload` is not a parameter: this flow stays on the machine.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .archive import ArchiveReport, archive_sync_output, format_placement
from .transcribe import (
    TranscriptOutcome,
    TranscriptionEngine,
    transcribe_tree,
    transcript_summary,
)


class SyncFailed(Exception):
    """The existing sync command failed. Staging files are kept."""

    def __init__(self, staging: Path, original: BaseException):
        self.staging = staging
        self.original = original
        super().__init__(
            "Download failed. Any audio captured so far is in "
            f"{staging}. {original}"
        )


@dataclass
class SyncReport:
    library: Path
    archive: ArchiveReport
    transcripts: list[TranscriptOutcome]

    @property
    def failed(self) -> bool:
        if any(item.action == "failed" for item in self.transcripts):
            return True
        return any(item.action == "missing" for item in self.archive.placements)


def format_report(report: SyncReport) -> str:
    filed = len(report.archive.recordings("filed"))
    skipped = len(report.archive.recordings("skipped"))
    summary = transcript_summary(report.transcripts)
    return (
        f"Done. Filed {filed} recording(s), "
        f"skipped {skipped} already archived. "
        f"{summary}"
    )


def _cleanup_staging(staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.parent.rmdir()
    except OSError:
        pass


def run_sync_and_transcribe(
    *,
    address: str,
    library: Path,
    sync_fn: Callable[..., None],
    engine: TranscriptionEngine,
    key_path: Optional[str] = None,
    push_key: bool = False,
    idle: float = 5.0,
    include_logs: bool = False,
    force: bool = False,
    now: Optional[datetime] = None,
    on_event: Optional[Callable[[str], None]] = None,
) -> SyncReport:
    """Download with the existing sync command, file by date, then transcribe.

    `sync_fn` is called with the existing sync's safe arguments only:
    address, out_path, key_path, push_key, idle, include_logs.
    """
    library = Path(library)
    library.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    staging = library / ".staging" / f"{stamp}-{os.getpid()}"
    staging.mkdir(parents=True, exist_ok=True)
    capture_base = staging / "capture.opus"

    def emit(message: str) -> None:
        if on_event:
            on_event(message)

    emit(f"Downloading recordings from {address} into {library}.")
    try:
        sync_fn(
            address=address,
            out_path=str(capture_base),
            key_path=key_path,
            push_key=push_key,
            idle=idle,
            include_logs=include_logs,
        )
    except Exception as exc:
        raise SyncFailed(staging, exc) from exc

    emit("Filing recordings into dated folders.")
    archive = archive_sync_output(capture_base, library, now=now)
    for placement in archive.placements:
        emit(format_placement(placement, library))

    missing = [item for item in archive.placements if item.action == "missing"]
    if not missing:
        _cleanup_staging(staging)
    else:
        emit(f"Left the download in {staging} because a file was missing.")

    emit("Transcribing. Recordings that already have a transcript are skipped.")
    transcripts = transcribe_tree(
        library,
        engine=engine,
        force=force,
        on_event=on_event,
    )
    return SyncReport(library=library, archive=archive, transcripts=transcripts)
