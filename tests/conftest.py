import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
from src.ledger import Ledger
from src.portfolio import Portfolio


@pytest.fixture
def clean_config(monkeypatch):
    """Pin every config value the portfolio math depends on, so tests don't
    drift when defaults are re-tuned. Slippage off — it has its own test."""
    monkeypatch.setattr(config, "STARTING_CAPITAL", 10_000.0)
    monkeypatch.setattr(config, "POSITION_SIZE_PCT", 0.15)
    monkeypatch.setattr(config, "MAX_POSITIONS", 8)
    monkeypatch.setattr(config, "CRYPTO_MAX_CAPITAL_PCT", 0.30)
    monkeypatch.setattr(config, "FEE_SLIPPAGE_PCT", 0.0)
    # Regulatory fees off by default so exact-PnL assertions stay clean;
    # the fee-specific tests re-enable them explicitly.
    monkeypatch.setattr(config, "ALPACA_SEC_FEE_RATE", 0.0)
    monkeypatch.setattr(config, "ALPACA_FINRA_TAF_PER_SHARE", 0.0)
    monkeypatch.setattr(config, "ALPACA_FINRA_TAF_CAP", 0.0)
    monkeypatch.setattr(config, "LEVERAGE", 1.0)
    monkeypatch.setattr(config, "MARGIN_CALL_LOSS", 0.9)
    monkeypatch.setattr(config, "TAKE_PROFIT_R_MULT", 3.0)
    monkeypatch.setattr(config, "PROFIT_TRAIL_TRIGGER_R", 1.5)
    monkeypatch.setattr(config, "PROFIT_TRAIL_DISTANCE_R", 1.5)
    return config


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "test.db"))


@pytest.fixture
def portfolio(ledger, clean_config):
    return Portfolio(ledger, starting_capital=10_000.0)
