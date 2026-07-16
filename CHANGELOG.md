# Patch Notes

## 2026-07-16 — Orphaned trades manage themselves

### Fixed

- **Orphaned ledger trades froze forever under a managing broker.** Trades
  opened by the simulator before a switch to `BROKER=alpaca` have no
  server-side orders backing them, so broker-side exits could never fire —
  observed live: five crypto positions sat frozen for days, two of them
  *past their take-profit* and one *below its stop*, holding 74% of capital.
  Now, once the broker denies holding a position for 3 consecutive monitor
  cycles, the trade is managed locally with simulated fills — stops,
  targets, trailing and EOD liquidation all work again until it closes.
  Transport errors don't count toward (or reset) the orphan verdict, so a
  flaky network can't misclassify a real position.

## 2026-07-08 — Crypto capital cap

### Fixed

- **Crypto could starve the stock session of capital.** Crypto scans run
  24/7 while stocks only trade 9:30–16:00 ET, so with `ENABLE_CRYPTO=1` the
  bot would fill position after position overnight and open the stock
  session with no buying power left. Crypto's total committed margin is now
  capped at `CRYPTO_MAX_CAPITAL_PCT` of capital (default 0.30 → two
  concurrent crypto positions at 15% sizing); the rest stays reserved for
  stocks. Reminder: crypto remains **off by default** — its entry rules
  tested negative-edge in validation.

## 2026-07-06 — Alpaca-only, with modeled trading costs

### Added

- **README.md** — public-facing setup guide: local install, `.env` creation
  with Alpaca paper keys, Docker usage, backtesting and test commands, and a
  link to the live instance at trader.marwansummakieh.me.

### Removed

- **eToro integration** deleted entirely — `EToroClient`, its config keys
  (`ETORO_PUBLIC_KEY` / `ETORO_PRIVATE_KEY`), the startup balance display,
  the unused `_etoro` handle in the server, and the now-dead `requests` /
  `uuid` imports in `src/data.py`. eToro was only ever a read-only balance
  readout and was never in the execution path, so nothing about trading
  changes. Alpaca is now the sole broker; the internal paper simulator
  (`SimBroker`, `BROKER=paper`) remains as the default test/paper backend.

### Added

- **Alpaca cost model** (`src/fees.py`): Alpaca US equities are
  commission-free, so the real costs are the bid/ask spread (already modeled
  per side by `FEE_SLIPPAGE_PCT`) plus small regulatory pass-through fees on
  **sells only** — SEC fee (fraction of proceeds) + FINRA TAF (per share,
  capped). Config: `ALPACA_SEC_FEE_RATE`, `ALPACA_FINRA_TAF_PER_SHARE`,
  `ALPACA_FINRA_TAF_CAP` (all `.env`-overridable when rates change).
  - Applied on every close in both the live/paper path (`Portfolio` routes
    all closes through `_record_close`, netting the fee into the exit price)
    and the backtest simulator (`SimParams` fee fields; `BTTrade.pnl` nets a
    per-trade fee), so realized PnL is honest net of every real cost.
  - Re-validated: the strategy edge is unchanged — 395 trades, +$1,822 net
    of fees (was +$1,841 gross), still +0.14R/trade. Fees cost ~$0.05/trade.
    The 0.1%/side spread assumption is conservative for this liquid universe
    now that no commission is bundled into it.

## 2026-07-05 — Mobile dashboard, tabbed layout, daily P&L metrics

### Added

- **Tabbed dashboard**: Positions, Scans and Orders now live in three tabs
  with live counts; the active tab persists across page reloads. The orders
  tab shows exit-reason badges for every close type (Target / Stop / Trail /
  Margin / EOD / Manual) plus a best/worst/avg summary line.
- **Daily gain metrics** replace the win-rate / total-trades tiles:
  - *Today Realized* — closed P&L for the current ET day, with trade count
    and win rate.
  - *Today Est.* — realized + unrealized open P&L, with % of day-start
    capital and the open-position component broken out.
  - *All-Time P&L* — keeps the removed trade count / win rate in its
    sub-label, so nothing is lost.
  - Backing API: `Ledger.get_stats_for_day`, a `today` block in
    `/api/stats`, and `unrealized_pnl` in `/api/status`.
- **Phone-friendly layout**: stat grid reflows 6 → 3 → 2 columns, position
  cards go two-column, the orders table drops secondary columns
  (type/exit/%/opened) to fit a 375px screen without horizontal scrolling,
  and tab buttons expand to full width.

### Added (deployment)

- **Version stamp** (`config.VERSION`): printed at bot startup, shown in the
  dashboard header, and returned by `/api/status` — so "which build is this
  deployment actually running?" is answerable at a glance. Bump it when
  cutting a release. Note for Docker deploys: code is baked into the image
  at build time, so uploading new files does nothing until you rebuild —
  `docker compose up -d --build`.
- `.dockerignore` now excludes `.venv/`, `cache/`, SQLite WAL files and
  local agent config, keeping the build context and image small.

### Added (migration tooling)

- `close_all.py` — winds down every open ledger trade at current prices
  (paper-sim fills, reason `manual_close`), preserving history and realized
  PnL. Run it before switching `BROKER` so simulator-era positions don't
  hang open under a broker that never opened them:
  `docker compose run --rm bot python close_all.py --yes`

### Fixed

- **Ledger concurrency**: the dashboard fires five API requests in parallel
  and FastAPI serves them from a threadpool, but all endpoints shared one
  SQLite connection — concurrent access raised
  `sqlite3.InterfaceError: bad parameter or other API misuse` (surfacing as
  intermittent 500s / "Server unreachable" flashes, also possible on the old
  dashboard). All ledger DB access is now serialized behind a lock, with a
  parallel-hammer regression test.
- Order timestamps rendered a raw ISO "T" separator (`07-05T13:02`).

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
