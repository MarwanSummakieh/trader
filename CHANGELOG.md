# Patch Notes

## 2026-08-20 — Volatility regime gate for stock entries

### Added

- **ATR volatility gate** (`REQUIRE_ATR_STOP`, default on): stocks are only
  entered when the ATR term actually sets the stop — `1.5*ATR(5m) >
  STOP_LOSS_PCT * price`. When intraday volatility is below that line, the
  stop is pinned at the 2% floor, R stops being volatility-scaled, and the
  3R target sits several days of range away — those entries spent July and
  August churning to 15:45 EOD scratches (live ledger and backtest agree:
  of the last 40 live trades, 36 were `eod_close` netting ~$0 and none
  reached take-profit or trail). New `Analysis.atr_binding` feature
  (fail-closed on ATR warmup, stocks only — crypto has its own rule),
  enforced in the scanner and mirrored in the backtest simulator
  (`SignalData.atr_ok`, `SimParams.require_atr_stop`); low-vol scans are
  labeled in the dashboard signal text.

### Research (2026-08-20, 59d of 5m bars ending 08-19, train/holdout at 07-23)

- The validated momentum edge **decayed across the window**: +0.15R →
  +0.02R → −0.15R per walk-forward third; in the holdout month **no tested
  long rule earned a single take-profit or trail exit**.
- Fresh challenger families, all negative on train — recorded in
  `config.py` so they aren't re-tried: EMA21-pullback reclaim, VWAP
  reclaim, RSI-30 dip, lower-band reclaim, opening-range breakout (retest),
  prior-day-high break, 8h-high stock breakout, BB-squeeze breakout,
  gap-and-go, gap-fade, VWAP-hold, 3-bar momentum burst.
- Softening the stop floor to 1% (keep trading quiet tape with smaller R)
  loses: fees/slippage dominate small R. Removing the 15:45 EOD liquidation
  is a wash on totals, doubles per-trade expectancy but locks capital
  overnight, worsens drawdown, and takes uncapped gap risk (worst observed
  fill: 3.8R below the stop) — rejected.
- The gate itself is a **plateau, not a tuned point**: every threshold from
  0.6× to 1.2× of the floor is positive on train (+0.23R to +0.50R/trade)
  and takes **zero trades in the holdout month** — standing aside in a
  regime where everything lost. Honesty note: the gated train subset is 35
  trades, 95% CI [−0.20R, +0.65R] — treat the gate as preserving the
  validated edge's operating conditions and preserving capital, not as
  fresh proven alpha. Expect multi-day flat stretches while volatility is
  dead; the exit set (3R target, 1.5R/1.5R trail) and RSI/ADX bands keep
  their previously validated values (re-tuning them on 35 trades would be
  overfitting).

## 2026-07-22 — A crypto-native entry rule (regime-gated 8h breakout)

### Changed

- **Crypto no longer trades the stock momentum rule.** New crypto-only
  entry (`src/scanner.py::_crypto_entry_ok`): last completed 5m close above
  the prior `CRYPTO_BREAKOUT_BARS` (96 bars = 8h) high, own daily-EMA50
  regime (deliberately not toggleable — the gates ARE the strategy), BTC
  above its daily EMA50 (`CRYPTO_REQUIRE_BTC_UPTREND`, fails closed when
  BTC is missing from a scan), and volume ≥ `CRYPTO_MIN_VOL_RATIO` (1.2x
  20-bar average). Exits unchanged: ATR stop, 3R target, 1.5R/1.5R trail.
- **Research verdict, kept honest.** Four long-only families tested on 59d
  of 5m bars (train/holdout split) and 1y of daily bars, in a tape where
  every universe coin fell 45–80% (max DD to -85%):
  - stock momentum rule on crypto: **-0.23R** over 121 train trades
  - 5m mean reversion (band reclaim / RSI turn / dip-buy): ≈0 or negative
    in every variant
  - daily Donchian trend-following (10/20/30/55d): **-0.8R and worse** —
    bear rallies print new daily highs, then collapse
  - **this rule: +0.26R (13 trades) train, -0.13R (11 trades) holdout** —
    the July 15 breakout cluster reversed; the July 19-20 cluster was in
    profit at window end. Full 59d: 24 trades, +0.08R, PF 1.13, DD -3.7%.
  The edge is **unproven** (tiny samples by construction — the gates keep
  it flat most of the time). Adopted because it is strictly safer than the
  stock rule the crypto instance previously ran (~2% max backtest DD vs
  ~8% steady bleed) and holds near-zero exposure outside confirmed
  uptrends — the only condition in which any tested long rule made money.
  Treat live results as data collection.
- **Backtest engine parity.** `build_signal_frame` exposes `brk_ok`,
  `simulate()` applies the crypto rule to crypto frames (BTC gate fails
  closed, mirroring the scanner), and `slice_frames` carries the new
  column — `python backtest.py --crypto-only` now backtests the live
  crypto strategy, not the stock rule.
- The analyzer exposes `breakout_ok` (False during indicator warmup — an
  unknown breakout level blocks entries) and tags crypto scan results with
  an "8h-high breakout" signal for the dashboard.

### Added

- Tests for every crypto entry clause: breakout required, own regime not
  bypassable via `REQUIRE_DAILY_UPTREND`, inclusive volume floor, BTC gate
  fail-closed / uptrend / config-off, and independence from the stock
  momentum clauses.
- **Crypto scan cadence made structurally 24/7** (`src/bot.py::
  universes_to_scan`). Behavior was already round-the-clock, but the
  universe selection lived inside the market-hours branch where it read as
  session-gated, and a crypto-only instance's console header showed
  "Market: CLOSED" as if it were waiting for the open — it now shows
  "Crypto: 24/7". Pinned by tests so a regression can't leave the crypto
  instance idle until 9:30 ET.

## 2026-07-22 — Separate stock and crypto instances

### Added

- **Two bot instances, two ledgers.** docker-compose now starts `bot`
  (stocks, $10k, Alpaca bracket orders, crypto pinned off) and `bot-crypto`
  (crypto-only, $1,000, own `ledger-crypto.db`, scanning 24/7). New
  `ENABLE_STOCKS` toggle mirrors `ENABLE_CRYPTO` so an instance trades a
  single asset class; the ENABLE_* flags are pinned per-service in compose
  so a stray `.env` value can't make both instances trade the same class.
  The crypto instance runs `BROKER=paper` (AlpacaBroker is stocks-only — no
  crypto symbology or bracket support) and `CRYPTO_MAX_CAPITAL_PCT=1.0`,
  since there is no stock session to reserve capital for.
- **`/crypto` dashboard page.** One server process reads both ledgers:
  `/` + `/api/*` serve the stock instance, `/crypto` + `/api/crypto/*` the
  crypto instance (`CRYPTO_DB_PATH`, `CRYPTO_STARTING_CAPITAL`). Same
  single-page app — it switches API base on its pathname — with a
  STOCKS/CRYPTO switcher in the header and an always-on "CRYPTO 24/7"
  market pill on the crypto page.
- **Server tests** (`tests/test_server.py`): per-instance ledger isolation,
  starting capitals, the 24/7 market flag, and the `/crypto` page route.
  New dev dependency `httpx2` (fastapi TestClient transport).

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
  local configuration, reducing the build context and image size.

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
