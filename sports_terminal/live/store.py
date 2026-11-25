"""REIMPLEMENTATION (minimal) of the JSON-lines store that paper.py writes through.

The private store adds disk-budget checks, manifests with file hashes, and health logs
tied to the server. Here: one append-only file per stream per UTC hour, with the same
``write(stream, row)`` / ``health(...)`` / ``close()`` calls, so paper.py runs unchanged.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


class JsonlStore:
    def __init__(self, root: Path, guard=None, suffix: str = "") -> None:
        self.root = Path(root)
        self.suffix = suffix
        self.counts: Counter[str] = Counter()
        self._handles: dict[tuple[str, str], object] = {}

    def _handle(self, stream: str, ts_ms: int):
        hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H")
        key = (stream, hour)
        if key not in self._handles:
            folder = self.root / stream
            folder.mkdir(parents=True, exist_ok=True)
            self._handles[key] = open(folder / f"{hour}{self.suffix}.jsonl", "a", encoding="utf-8")
        return self._handles[key]

    def write(self, stream: str, row: dict) -> None:
        ts_ms = int(row.get("ts_ms") or row.get("trade_ts_ms") or 0)
        handle = self._handle(stream, ts_ms)
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        handle.flush()
        self.counts[stream] += 1

    def health(self, source: str, event: str, detail: str = "", **extra) -> None:
        return None  # health log omitted in the showcase

    def disk_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def write_manifest(self):
        return None  # manifests omitted in the showcase

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()
