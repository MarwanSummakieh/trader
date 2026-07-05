# Patch Notes

## 2026-07-04 — Alpaca execution, strategy re-validation, test suite

### Added

**Broker execution layer** ([src/broker.py](src/broker.py))
- New `Broker` interface between the Portfolio (bookkeeping) and order
  execution. Selected via `BROKER` env var; the Portfolio/ledger stay
  broker-agnostic.
- `SimBroker` (`BROKER=paper`, default): instant simulated fills with the
  same slippage model the backtest uses. Behaviour is identical to before —
  existing ledgers and stats are unaffected.
- `AlpacaBroker` (`BROKER=alpaca`): submits server-side **bracket orders**
  (entry + stop-loss + take-profit resting at the broker), so exits fire even
  if the bot process dies or its data feed stalls. The R-based trailing stop
  raises the bracket's stop leg via order replace; EOD liquidation cancels
  the legs and market-closes. Exits executed by the broker are reconciled
  into the ledger with real fill prices (order-type → exit-reason mapping,
  including the `trail_stop` relabel).
- New env vars: `BROKER`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
  `ALPACA_BASE_URL` (defaults to the paper endpoint). A non-paper endpoint
  is refused unless `ALPACA_ALLOW_LIVE=1` is set explicitly.
- Guardrails: bracket orders require whole shares (fractional quantities are
  rounded down; 0-share positions skipped); crypto symbols are refused
  (stocks only); unfilled entry orders are cancelled and any partial fill is
  flattened; transport errors are never mistaken for "position closed".
- Bot startup verifies broker connectivity and shows account equity; the
  dashboard header shows the active execution backend.
- **Existing ledgers carry over unchanged** — no schema changes, so a
  `ledger.db` from an earlier deployment keeps its capital, stats and open
  positions. Startup flags any ledger trade a managing broker has no
  position for (e.g. open paper trades carried into a `BROKER=alpaca`
  switch), since the broker can never close what it never opened — wind
  those down with `BROKER=paper` first.
- Known limits of the Alpaca path (real money, not paper): the US Pattern
  Day Trader rule caps margin accounts under $25k at ~3 day trades per 5
  sessions — this strategy does ~6-7/day, so live trading needs $25k+, a
  cash account (settlement constraints), or a CFD broker. CFD-style
  `LEVERAGE`/`MARGIN_CALL_LOSS` semantics apply only to the simulator.

**Test suite** ([tests/](tests), 48 tests, `pytest`; deps in
[requirements-dev.txt](requirements-dev.txt))
- Portfolio: every exit branch (stop / target / trailing / margin-call /
  EOD), sizing, slippage fills, capital accounting.
- Scanner: every clause of the validated entry rule, including boundary
  values, the entry cutoff, and the earnings filter.
- Backtest simulator: fidelity rules pinned (next-bar-open entries,
  stop-before-target pessimism, entry-bar exit skip, same-session signals,
  EOD liquidation, gap-through-stop handling).
- Broker layer: Portfolio↔broker contract (server-side exits are
  authoritative; ledger never claims a tighter stop than the broker holds)
  and AlpacaBroker REST behaviour against a stubbed transport.

### Changed

- **Strategy re-validated 2026-07-04** (59 days of 5m bars, train/holdout +
  walk-forward): baseline momentum rule confirmed at **+0.14R/trade over
  395 trades** (95% CI [+0.03, +0.26], P(edge≤0) < 1%), and it beat ~20
  challengers out-of-sample. Rejected (recorded in [config.py](config.py) so
  they aren't re-tried): volume-ratio/VWAP/MACD entry filters,
  skip-first-30-min, relative-strength vs SPY, SPY regime gate (untestable —
  no bear regime in window), time-boxed exits, opening-range breakout family,
  RSI dip-buy family. Exit parameters (3R target, 1.5R/1.5R trail) confirmed
  on a robust plateau. No parameter changes.
- `MONITOR_INTERVAL_SECS` 120 → 60: the backtest assumes stops are caught
  within one 5m bar; the monitor only quotes open positions (≤8 tickers
  behind a 20s price cache), so faster polling is free fidelity. (Moot under
  Alpaca, where stops rest server-side.)
- eToro client note: integration is read-only (startup balance display);
  execution now goes through the broker layer above.

### Fixed

- `backtest.py` CLI crashed with a `UnicodeEncodeError` on legacy Windows
  (cp1252) consoles due to a `≥` character in the report output.
