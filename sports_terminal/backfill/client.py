"""STUB. The private repo's rate-limited Kalshi reader is not published.

paper.py and ledger.py construct KalshiHistoryClient only when they run against the live
public API. The showcase reproduces every number from data/sample/ offline, so none of
those code paths is exercised here. Calling any method raises, so a missing client cannot
fail silently.
"""
from __future__ import annotations


class KalshiHistoryClient:
    def __init__(self, *args, **kwargs) -> None:
        self._args = args
        self._kwargs = kwargs

    async def get(self, path: str, params: dict | None = None):
        raise NotImplementedError(
            "The live public-API reader is not included in this showcase. "
            "Use data/sample/ with scripts/reproduce_headline.py instead.")

    async def aclose(self) -> None:
        return None
