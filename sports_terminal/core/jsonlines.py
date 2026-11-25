"""Tolerant JSON-lines reader. REIMPLEMENTATION: the private repo's version also records
bad lines to a health log, which is omitted here. Behaviour used by gas/tape.py is kept:
a line that does not parse as a JSON object returns None and is skipped."""
from __future__ import annotations

import json
from pathlib import Path


def loads(line: str | bytes, *, source: str = "") -> dict | None:
    text = line.strip() if isinstance(line, str) else line.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = loads(line, source=str(path))
            if row is not None:
                rows.append(row)
    return rows
