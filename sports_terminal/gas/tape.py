"""Reading the JSONL tape, including the parts rotation has already compressed.

The store writes one file per stream per UTC hour. ``gas-rotate`` gzips any hour-file
older than six hours, so on a server that has been up for a day most of the tape is
``*.jsonl.gz`` and only the newest hour or two is still plain ``*.jsonl``.

Every reader must therefore look at both. A reader that globs ``*.jsonl`` alone does not
fail -- it quietly returns whatever has not been compressed yet, which looks like a
working report with a suspiciously short history. That is exactly the bug this module
exists to make impossible: the daily ledger showed every fill at ET hour 23 because the
only uncompressed file left was 03:00-03:59 UTC.

If both ``X.jsonl`` and ``X.jsonl.gz`` exist -- which happens if rotation was interrupted
between writing the archive and deleting the original -- the plain file wins and the
archive is ignored, so the rows are read once rather than twice.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from ..core import jsonlines

SUFFIX = ".jsonl"
ARCHIVE_SUFFIX = ".jsonl.gz"


def hour_files(directory: Path) -> list[Path]:
    """Every hour-file in one stream directory, oldest first, each counted once."""
    if not directory.is_dir():
        return []
    chosen: dict[str, Path] = {}
    for path in directory.iterdir():
        name = path.name
        if name.endswith(ARCHIVE_SUFFIX):
            key = name[: -len(ARCHIVE_SUFFIX)]
            chosen.setdefault(key, path)
        elif name.endswith(SUFFIX):
            key = name[: -len(SUFFIX)]
            chosen[key] = path  # a live file always beats its archive
    return [chosen[key] for key in sorted(chosen)]


def _open(path: Path):
    if path.name.endswith(ARCHIVE_SUFFIX):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def read_rows(root: Path, stream: str) -> Iterator[dict]:
    """Every row of one stream, oldest first. Unreadable lines are skipped."""
    for path in hour_files(Path(root) / stream):
        try:
            with _open(path) as handle:
                for line in handle:
                    row = jsonlines.loads(line, source=str(path))
                    if row is not None:
                        yield row
        except OSError:
            continue


def tail_rows(root: Path, stream: str, count: int) -> list[dict]:
    """The last ``count`` rows, reading only as many hour-files as that needs."""
    files = hour_files(Path(root) / stream)
    out: list[dict] = []
    for path in reversed(files):
        rows: list[dict] = []
        try:
            with _open(path) as handle:
                for line in handle:
                    row = jsonlines.loads(line, source=str(path))
                    if row is not None:
                        rows.append(row)
        except OSError:
            continue
        out = rows + out
        if len(out) >= count:
            break
    return out[-count:]


def last_row(root: Path, stream: str) -> dict | None:
    rows = tail_rows(root, stream, 1)
    return rows[-1] if rows else None
