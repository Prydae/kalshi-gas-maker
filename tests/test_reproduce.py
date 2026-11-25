"""The public sample must reproduce the full-run figures for the five evenings it covers."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location("reproduce", ROOT / "scripts" / "reproduce_headline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sample_reproduces_pinned_evenings():
    assert _load_script().main() == 0


def test_taker_fee_is_the_published_rate():
    from sports_terminal.core.config import FeeSettings
    from sports_terminal.core.fees import kalshi_fee

    settings = FeeSettings()
    # 0.07 x 1 x 0.50 x (1 - 0.50) = 0.0175, rounded up to the next cent = 0.02 dollars per contract
    assert kalshi_fee(0.50, 1, True, settings=settings) == 0.02
