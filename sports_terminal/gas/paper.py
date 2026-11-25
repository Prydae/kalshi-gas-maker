"""Forward paper market maker for the US national gas ladder. Records only.

    python -m sports_terminal.gas.paper --root data_gas/paper

**This places no orders.** It reads the public book, writes what a resting offer would
have done, and nothing else. There is no auth, no signing key, no POST, and no import of
anything that could write an order -- ``tests/test_gas_paper_guard.py`` fails the build if
that changes.

How a "sell YES at P" order actually rests
------------------------------------------
Kalshi's book has two bid sides and no ask side. ``yes_dollars`` are bids to buy YES at
p; ``no_dollars`` are bids to buy NO at q, and buying NO at q is the same trade as selling
YES at 1-q. So the YES offers are the NO bids read backwards: the best YES ask is
``1 - max(no price)``, and an offer of ours to sell YES at P rests in the NO book at
``q = 1 - P``. The size already at that level is the size we queue behind.

The queue model is deliberately strict
--------------------------------------
We join **behind** everything resting at our price. That queue only shrinks here when a
YES-buying taker actually consumes it: a print at our price eats into the size ahead, and
a print through our price means the level was cleared and we are done. Cancellations
ahead of us would let us up the queue sooner and are invisible in public data, so they are
not modelled -- which makes every fill here slower than reality, not faster. A print with
the taker on the NO side never fills us; it happened on the other side of the book.

Re-pricing loses the queue. When the best ask moves, the old order is cancelled and the
new one joins the back of the new level, exactly as it would in practice.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..backfill.client import KalshiHistoryClient
from ..live.store import JsonlStore

log = logging.getLogger("sports_terminal.gas.paper")

SERIES = "KXAAAGASD"
ET = ZoneInfo("America/New_York")
"""A real timezone, not a fixed -4 offset. The market's window is defined in New York
wall-clock time and the server may be anywhere; a fixed offset would silently shift the
whole session by an hour on the first Sunday in November and keep running as if nothing
had happened."""

POLL_SEC = 5.0
SIZES = (1, 10)
START_HOUR_ET = 18
IDLE_POLL_SEC = 60.0
"""Outside the session there is nothing to quote, so the loop sleeps rather than asking
the exchange for a market list 17,000 times a day to be told the same thing."""

TICK = 0.01


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_levels(raw) -> list[tuple[float, float]]:
    """``[["0.0100", "2030.06"], ...]`` -> [(price, size)], sorted by price."""
    out: list[tuple[float, float]] = []
    for item in raw or []:
        try:
            out.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort()
    return out


@dataclass
class Book:
    best_bid: float | None
    best_ask: float | None
    ask_size: float
    """Size resting at the best YES offer -- the NO bid at ``1 - best_ask``."""

    bid_size: float
    levels: int
    yes_levels: list = field(default_factory=list)
    no_levels: list = field(default_factory=list)
    """Every price level with its size, kept so the tape is the raw book and not a
    top-of-book summary. A summary cannot answer a question asked later -- how deep the
    queue was three levels down, say -- and the book cannot be re-fetched after the fact."""

    @classmethod
    def from_payload(cls, payload: dict) -> Book:
        inner = payload.get("orderbook_fp") or payload.get("orderbook") or {}
        yes = parse_levels(inner.get("yes_dollars"))
        no = parse_levels(inner.get("no_dollars"))
        best_bid = yes[-1][0] if yes else None
        bid_size = yes[-1][1] if yes else 0.0
        best_ask = round(1.0 - no[-1][0], 2) if no else None
        ask_size = no[-1][1] if no else 0.0
        return cls(best_bid, best_ask, ask_size, bid_size, len(yes) + len(no),
                   yes_levels=yes, no_levels=no)

    def size_at_offer(self, price: float) -> float:
        """How much is already offered at YES price ``price``. Only the top level is
        needed for our purpose, so anything deeper reports zero size ahead."""
        if self.best_ask is not None and abs(self.best_ask - price) < 1e-9:
            return self.ask_size
        return 0.0


@dataclass
class Offer:
    """One virtual resting offer: sell YES at ``price``, for one size bucket."""

    ticker: str
    size_cap: int
    price: float
    queue_ahead: float
    placed_ms: int
    initial_queue: float
    filled: float = 0.0
    fills: int = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.size_cap - self.filled)

    @property
    def done(self) -> bool:
        return self.remaining <= 0.0


@dataclass
class Position:
    """Everything we have sold on one strike, awaiting settlement."""

    ticker: str
    size_cap: int
    contracts: float = 0.0
    proceeds: float = 0.0
    """Sum of price x size. Divide by contracts for the average price received."""

    fills: int = 0
    by_hour: dict = field(default_factory=dict)


class PaperMaker:
    def __init__(self, root: Path, *, poll_sec: float = POLL_SEC,
                 start_hour: int = START_HOUR_ET) -> None:
        self.store = JsonlStore(root)
        self.root = Path(root)
        self.poll_sec = poll_sec
        self.start_hour = start_hour
        self.client = KalshiHistoryClient(requests_per_sec=12.0, burst=12.0)
        """A cycle is ~27 order books plus a trades call for each strike we are actually
        quoting. At 12/s that fits inside the 5-second poll; at 8/s it did not, and the
        measured cycle was 15s, which is long enough for a queue to move unobserved."""
        self.stopping = asyncio.Event()
        self.offers: dict[tuple[str, int], Offer] = {}
        self.positions: dict[tuple[str, int], Position] = {}
        self.seen_trades: set[str] = set()
        self.cycles = 0
        self.errors = 0
        self._state_path = self.root / "_paper_state.json"
        self._load_state()

    # ---------------------------------------------------------------- state

    def _load_state(self) -> None:
        """Restart-safe: positions and open offers survive a restart.

        Fills are the thing that must not be double-counted, so trade ids we have
        already applied are persisted too.
        """
        if not self._state_path.is_file():
            return
        try:
            blob = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("state file unreadable, starting clean")
            return
        for row in blob.get("positions", []):
            key = (row["ticker"], row["size_cap"])
            self.positions[key] = Position(**row)
        for row in blob.get("offers", []):
            key = (row["ticker"], row["size_cap"])
            self.offers[key] = Offer(**row)
        self.seen_trades = set(blob.get("seen_trades", []))
        log.info("resumed: %d positions, %d offers, %d trade ids",
                 len(self.positions), len(self.offers), len(self.seen_trades))

    def _save_state(self) -> None:
        payload = {
            "positions": [asdict(p) for p in self.positions.values()],
            "offers": [asdict(o) for o in self.offers.values()],
            # only recent ids are needed; the trades feed is newest-first and bounded
            "seen_trades": list(self.seen_trades)[-20000:],
            "saved_ms": now_ms(),
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._state_path)

    # ---------------------------------------------------------------- data

    async def open_markets(self) -> list[dict]:
        payload = await self.client.get(
            "/markets", {"series_ticker": SERIES, "status": "open", "limit": 200})
        return payload.get("markets") or []

    async def orderbook(self, ticker: str) -> Book | None:
        try:
            payload = await self.client.get(f"/markets/{ticker}/orderbook", {"depth": 100})
        except Exception as exc:  # noqa: BLE001 - one bad book must not end the run
            self.errors += 1
            self.store.health("paper", "error", f"{ticker} book: {type(exc).__name__}: {exc}")
            return None
        return Book.from_payload(payload)

    async def recent_trades(self, ticker: str, since_ms: int) -> list[dict]:
        try:
            payload = await self.client.get(
                "/markets/trades",
                {"ticker": ticker, "limit": 100, "min_ts": max(0, since_ms // 1000 - 60)})
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self.store.health("paper", "error", f"{ticker} trades: {type(exc).__name__}: {exc}")
            return []
        return payload.get("trades") or []

    # ---------------------------------------------------------------- logic

    def apply_trade(self, ticker: str, price: float, count: float, taker: str,
                    ts_ms: int) -> None:
        """Walk our queue forward. Only a YES-buying taker can reach our offer."""
        if taker != "yes":
            return
        for size_cap in SIZES:
            offer = self.offers.get((ticker, size_cap))
            if offer is None or offer.done:
                continue
            if ts_ms < offer.placed_ms:
                # A print from before we joined cannot have consumed the queue we joined
                # behind -- that size was still there, which is why we measured it. The
                # trades feed is pulled with a lookback window, so this is not theoretical.
                continue
            if price < offer.price - 1e-9:
                continue  # took a better offer; our level was not reached
            if price > offer.price + 1e-9:
                # a print through our price means the whole level cleared, us included
                consumed = offer.queue_ahead
                offer.queue_ahead = 0.0
                available = offer.remaining
            else:
                consumed = min(count, offer.queue_ahead)
                offer.queue_ahead -= consumed
                available = min(offer.remaining, count - consumed)
            self.store.write("paper_queue", {
                "ticker": ticker, "size_cap": size_cap, "price": offer.price,
                "queue_ahead": offer.queue_ahead, "consumed": consumed,
                "trade_price": price, "trade_count": count, "trade_ts_ms": ts_ms,
                "initial_queue": offer.initial_queue,
            })
            if available <= 0:
                continue
            offer.filled += available
            offer.fills += 1
            hour = datetime.fromtimestamp(ts_ms / 1000, tz=ET).hour
            position = self.positions.setdefault(
                (ticker, size_cap), Position(ticker, size_cap))
            position.contracts += available
            position.proceeds += offer.price * available
            position.fills += 1
            position.by_hour[str(hour)] = position.by_hour.get(str(hour), 0) + available
            self.store.write("paper_fill", {
                "ticker": ticker, "size_cap": size_cap, "price": offer.price,
                "size": available, "trade_price": price, "trade_count": count,
                "trade_ts_ms": ts_ms, "hour_et": hour,
                "queue_at_placement": offer.initial_queue,
                "seconds_resting": max(0, (ts_ms - offer.placed_ms) // 1000),
            })
            log.info("FILL %s cap=%d %.2f x%.0f (queue was %.0f)",
                     ticker, size_cap, offer.price, available, offer.initial_queue)

    def reprice(self, ticker: str, book: Book) -> None:
        """Keep one offer per size bucket sitting on the best ask."""
        for size_cap in SIZES:
            key = (ticker, size_cap)
            offer = self.offers.get(key)
            target = book.best_ask
            # No offer at or below the best bid: that would be marketable, not resting.
            if target is None or book.best_bid is None or target <= book.best_bid:
                if offer is not None:
                    self.store.write("paper_quote", {
                        "ticker": ticker, "size_cap": size_cap, "action": "cancel",
                        "price": offer.price, "reason": "no valid ask above the bid"})
                    self.offers.pop(key, None)
                continue
            if offer is not None and not offer.done and abs(offer.price - target) < 1e-9:
                continue  # still at the right price, keep our place in line
            if offer is not None:
                self.store.write("paper_quote", {
                    "ticker": ticker, "size_cap": size_cap, "action": "cancel",
                    "price": offer.price, "reason": "ask moved" if not offer.done
                    else "filled", "queue_ahead": offer.queue_ahead})
            ahead = book.size_at_offer(target)
            self.offers[key] = Offer(
                ticker=ticker, size_cap=size_cap, price=target, queue_ahead=ahead,
                placed_ms=now_ms(), initial_queue=ahead)
            self.store.write("paper_quote", {
                "ticker": ticker, "size_cap": size_cap, "action": "place",
                "price": target, "queue_ahead": ahead, "best_bid": book.best_bid,
                "best_ask": book.best_ask})

    # ---------------------------------------------------------------- loop

    async def cycle(self, markets: list[dict]) -> None:
        for market in markets:
            if self.stopping.is_set():
                return
            ticker = market["ticker"]
            book = await self.orderbook(ticker)
            if book is None:
                continue
            self.store.write("paper_book", {
                "ticker": ticker, "best_bid": book.best_bid, "best_ask": book.best_ask,
                "ask_size": book.ask_size, "bid_size": book.bid_size,
                "levels": book.levels,
                # raw, every level with its size, as fetched
                "yes": book.yes_levels, "no": book.no_levels})
            offer = self.offers.get((ticker, SIZES[0]))
            if offer is None:
                # No live offer means no queue to advance, so the trades call for this
                # strike buys nothing. Skipping it is what keeps the cycle inside its
                # 5-second budget: 27 books plus a handful of trade calls, not 54 calls.
                self.reprice(ticker, book)
                continue
            for trade in await self.recent_trades(ticker, offer.placed_ms):
                trade_id = trade.get("trade_id")
                if not trade_id or trade_id in self.seen_trades:
                    continue
                self.seen_trades.add(trade_id)
                try:
                    price = float(trade.get("yes_price_dollars"))
                    count = float(trade.get("count_fp") or 0)
                except (TypeError, ValueError):
                    continue
                stamp = trade.get("created_time") or ""
                try:
                    ts_ms = int(datetime.fromisoformat(
                        stamp.replace("Z", "+00:00")).timestamp() * 1000)
                except ValueError:
                    ts_ms = now_ms()
                self.store.write("paper_trade", {
                    "ticker": ticker, "trade_id": trade_id, "yes_price": price,
                    "count": count, "taker_side": trade.get("taker_side"),
                    "ts_ms": ts_ms})
                self.apply_trade(ticker, price, count, str(trade.get("taker_side") or ""),
                                 ts_ms)
            self.reprice(ticker, book)

    async def run(self) -> None:
        self.store.health("paper", "start",
                          f"{SERIES} every {self.poll_sec}s from {self.start_hour}:00 ET")
        log.info("paper maker started; no orders are placed by this process")
        try:
            while not self.stopping.is_set():
                started = time.monotonic()
                hour = datetime.now(tz=ET).hour
                if hour < self.start_hour:
                    self.store.health("paper", "idle", f"hour {hour} ET, session not open")
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.stopping.wait(), IDLE_POLL_SEC)
                    continue
                try:
                    markets = await self.open_markets()
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    self.store.health("paper", "error",
                                      f"markets: {type(exc).__name__}: {exc}")
                    markets = []
                if markets:
                    await self.cycle(markets)
                    self.cycles += 1
                    self._save_state()
                    self.store.write_manifest()
                    if self.cycles % 12 == 0:
                        self.store.health(
                            "paper", "sweep",
                            f"cycle {self.cycles}, {len(markets)} strikes, "
                            f"{sum(p.fills for p in self.positions.values())} fills",
                            rows=dict(self.store.counts))
                else:
                    self.store.health("paper", "idle", f"hour {hour} ET, no open markets")
                elapsed = time.monotonic() - started
                wait = self.poll_sec if markets else IDLE_POLL_SEC
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), max(0.5, wait - elapsed))
        finally:
            self._save_state()
            self.store.health("paper", "stop", f"{self.cycles} cycles, {self.errors} errors")
            self.store.write_manifest()
            self.store.close()
            await self.client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sports_terminal.gas.paper")
    parser.add_argument("--root", type=Path, default=Path("data_gas/paper"))
    parser.add_argument("--poll", type=float, default=POLL_SEC)
    parser.add_argument("--start-hour", type=int, default=START_HOUR_ET)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    maker = PaperMaker(args.root, poll_sec=args.poll, start_hour=args.start_hour)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, maker.stopping.set)
            except (NotImplementedError, AttributeError, ValueError):
                signal.signal(sig, lambda *_: maker.stopping.set())
        await maker.run()

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    sys.exit(main())
