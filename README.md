# Trader

An automated intraday momentum trading bot for US equities, executing through
[Alpaca](https://alpaca.markets) paper trading with server-side bracket
orders, a live web dashboard, and a backtest engine that every strategy rule
was validated against. A second, independent instance trades crypto 24/7
with its own $1,000 ledger on the internal fill simulator.

**🔴 Live:** [trader.marwansummakieh.me](https://trader.marwansummakieh.me)
(stocks) · [/crypto](https://trader.marwansummakieh.me/crypto) (crypto)

> ⚠️ This project trades a **paper** (simulated) account. Nothing here is
> financial advice, and past backtest performance does not predict live
> results. If you point it at real money, that's on you.

## How it works

Every 5 minutes during market hours the bot scans a ~60-symbol universe of
liquid US stocks and enters positions that pass a backtest-validated momentum
rule:

- intraday EMA stack aligned (price > EMA9 > EMA21)
- daily ADX > 30 (trending, not chopping)
- RSI in [55, 70) — momentum, not blow-off
- price above its daily EMA50 (regime gate)
- enough intraday volatility that the ATR sets the stop, not the 2% floor
  (volatility gate — in dead tape the bot deliberately sits flat, sometimes
  for days, rather than churn scratch trades with no reachable target)
- no entries after 12:00 ET, no entries on earnings reaction days

Exits are ATR-anchored stops with a 3R take-profit and an R-based trailing
stop, all resting **server-side at the broker** as a GTC bracket order — so
stops fire even if the bot process dies. Since 2026-09-03 stock positions
are **held overnight** until the stop, target or trail fires (the old
15:45 flatten is available via `EOD_CLOSE_STOCKS=1`): with days of runway
the 3R target is actually reachable, which turned the July–August churn of
end-of-day scratches back into a positive expectancy in backtests.

A second stock entry family runs alongside momentum: a **swing pullback**
(Connors RSI(2) < 5 on the prior daily close, above the 200-day SMA),
entered in the first 30 minutes, held until the price is back above its
5-day average at the session end (or 10 sessions), with a 10% disaster
stop and 10% sizing. On two years of daily bars it earned ~+0.12R per
trade (R = 10%) with a 68% win rate, and it was the only tested rule still
positive in the dead-volatility summer of 2026.

Costs are modeled honestly: per-side spread/slippage plus Alpaca's sell-side
regulatory fees (SEC + FINRA TAF — Alpaca charges no commission). Every rule
was validated on train/holdout splits with walk-forward checks, net of
all costs; the full research notes (including everything that was tested
and rejected) live in [config.py](config.py) and [CHANGELOG.md](CHANGELOG.md).

The bot runs as **one instance per asset class**, each with its own ledger
and capital: the stock instance ($10k, Alpaca bracket orders, session hours)
and a crypto instance ($1k, internal simulator — Alpaca has no crypto
bracket support — scanning 24/7). `ENABLE_STOCKS` / `ENABLE_CRYPTO` select
what an instance trades.

The **crypto instance** trades its own rule, a regime-gated 8-hour
breakout: enter only when a coin's last completed 5-minute close breaks its
prior 8-hour high on above-average volume while **both** the coin and BTC
are above their daily EMA50s. Full disclosure: in a research window where
every universe coin fell 45–80%, *no* long-only crypto rule validated
positive — this one was adopted because its gates keep the instance nearly
flat outside confirmed uptrends (~2% max backtest drawdown vs ~8% steady
bleed for the old rule). Its edge is unproven; the $1k instance exists to
collect live evidence. The research notes live in [config.py](config.py)
and [CHANGELOG.md](CHANGELOG.md).

| Component | Role |
|---|---|
| Intraday trend | Closing price > EMA(9) > EMA(21) |
| Daily trend strength | ADX(14) > 30 |
| Intraday momentum | 55 <= RSI(14) < 70 |
| Daily price regime | Closing price > daily EMA(50) |
| Intraday volatility | 1.5 x ATR(14) > 0.02 x closing price |
| Price | At least USD 5 |
| Liquidity | Prior 20-session mean volume of at least 500,000 shares |
| Entry time | During the equity session and before 12:00 US Eastern |
| Earnings | Outside the configured earnings-reaction window |

The execution process requests liquidation from 15:45 US Eastern and retries
failed requests. This schedule does not guarantee an executed close at that
time. The composite technical score orders eligible candidates but is not an
entry threshold.

### 1.2 Cryptocurrency breakout

The cryptocurrency process evaluates signals continuously. Entry requires the
completed five-minute close to exceed the maximum high of the preceding 96 bars,
corresponding to eight hours. The current bar is excluded from that reference
interval. Bar volume must be at least 1.2 times its 20-bar rolling mean, which
includes the completed signal bar.

The asset price must exceed its prior daily EMA(50). By default, Bitcoin must
also exceed its own prior daily EMA(50); unavailable Bitcoin context prevents
entry. Cryptocurrency positions have no equity-session cutoff or scheduled
end-of-day liquidation.

### 1.3 Stops, targets, and position sizing

For signal price `P` and intraday average true range `ATR`, stop distance `d`,
initial stop level `S`, and target level `T` are:

```text
d = max(1.5 x ATR, 0.02 x P)
S = P - d
T = P + 3d
```

After execution at price `E`, initial risk per unit is `R = E - S`. This value
is stored independently of subsequent stop adjustments. Once the observed gain
reaches `1.5R`, the trailing stop may increase to `price - 1.5R`. Stops cannot
be lowered. Older records without stored initial risk retain the legacy
target-based approximation.

Default sizing limits are 15% of capital committed per position, 0.5% of capital
in estimated initial downside per trade, and 2.5% in aggregate reserved downside.
Capital is defined as starting capital plus realized profit or loss. Estimated
downside includes exit slippage and applicable equity sell fees. Open positions
reserve their initial downside after trailing-stop adjustments. Quantity is
also constrained by available capital; at most eight positions may coexist.

Default simulated leverage is one. A separate allocation limit applies to
cryptocurrencies. The risk fractions are configurable constraints rather than
empirically estimated optimal allocations. Gaps and unexpected fills can produce
losses above these limits. The Alpaca adapter rounds quantities down to whole
shares; the historical simulator permits fractional quantities.

## 2. Evaluation methodology

### 2.1 Data and execution assumptions

Historical evaluation uses five-minute OHLCV bars and daily context obtained
through `yfinance`. Downloads are cached locally. Snapshot filenames record
the requested period and acquisition date; reports record actual timestamps
and per-symbol coverage.

The simulator applies the following sequence:

1. Evaluate existing positions for opening gaps and scheduled liquidation.
2. Fill eligible signals from the preceding completed bar at the next bar's
   opening price, adjusted for slippage.
3. Evaluate intrabar stops and targets, including those on the entry bar.
4. Update trailing stops from closing prices for subsequent bars.

When stop and target are both reached within a bar and their order cannot be
inferred from the opening price, the stop is assumed to execute first. Capital
released by an intrabar exit cannot fund an entry at that bar's earlier open.
The equity series includes final liquidation costs, and daily returns include
the first evaluation day.

The default spread and slippage allowance is 0.1% per side. Equity sales also
incur configured SEC and FINRA TAF estimates. Cryptocurrency simulation excludes
these equity-specific fees. Venue commissions, funding, market impact, and
broker-specific fee rounding are not represented. Recorded fee parameters are
research assumptions rather than a date-specific reconstruction of charges.

### 2.2 Chronological parameter selection

The `--walk-forward` procedure uses the first half of available dates for
initial training and divides the remaining dates into three successive,
non-overlapping test blocks. Training expands before each block. Selection
maximizes training-period net return subject to at least 20 completed trades,
positive aggregate profit after modeled costs, and drawdown no greater than 10%.

If no candidate qualifies, the procedure holds cash for the next block.
Parameters are fixed before that block is evaluated. All comparison paths
liquidate at block boundaries, include the associated costs, and compound
capital between blocks. The default search evaluates nine equity combinations
of ADX threshold and target multiple, or three cryptocurrency target multiples.

Reports compare the selection procedure with the existing entry rule under
risk-limited and fixed-notional sizing. Evaluation does not modify execution
settings. Chronological separation does not eliminate selection bias from
repeated research on the same history. Bailey et al. (2017) examine this problem
in investment backtesting; the present implementation does not estimate their
probability of backtest overfitting statistic.

## 3. Recorded results and limitations

The 2026-09-03 snapshot contains 61 equity symbols and 13 cryptocurrency symbols.
Equity source coverage extends from 2026-06-11 to 2026-09-03; cryptocurrency
coverage extends from 2026-07-06 to 2026-09-03. Both datasets include a partial
final session. Starting capital is USD 10,000 for equities and USD 1,000 for
cryptocurrencies.

The following values summarize the concatenated test blocks. Drawdowns are
signed percentage changes from preceding equity peaks.

| Asset class | Evaluation path | Test trades | Test return | Maximum drawdown |
|---|---|---:|---:|---:|
| Equities | Existing rule, risk-limited sizing | 1 | -0.35% | -0.66% |
| Equities | Existing rule, fixed sizing | 1 | -0.35% | -0.66% |
| Equities | Parameter selection or cash | 0 | 0.00% | 0.00% |
| Cryptocurrencies | Existing rule, risk-limited sizing | 132 | +13.93% | -12.01% |
| Cryptocurrencies | Existing rule, fixed sizing | 132 | +13.93% | -12.01% |
| Cryptocurrencies | Parameter selection or cash | 42 | -0.98% | -9.37% |

The equity selection procedure held cash throughout the test period. A single
baseline equity trade is insufficient for statistical inference. Cryptocurrency
parameter selection underperformed the existing rule in this experiment. Risk
limits did not bind on the baseline test trades; the identical results do not
establish a performance benefit from risk-limited sizing.

Full parameters, interval boundaries, and coverage appear in the
[equity report](reports/stocks-walk-forward-20260903.json),
[cryptocurrency report](reports/crypto-walk-forward-20260903.json), and
[research note](reports/strategy-review.md).

The static symbol universe does not eliminate survivorship bias. Historical
earnings exclusions used by the execution process are not reproduced from the
OHLCV cache. Fractional simulated quantities and bar-level fills differ from
whole-share broker execution and its order lifecycle. The data were previously
examined during strategy development and are not an independent, previously
unexamined holdout.

Reported returns do not establish statistical significance, implementation
capacity, or prospective profitability. Research figures recorded before the
simulator corrections of 2026-09-06 require recomputation before comparison
with the present results.

## 4. Software architecture

| Component | Function |
|---|---|
| [main.py](main.py) | Process lifecycle, scanning, and position monitoring |
| [src/analyzer.py](src/analyzer.py) | Indicator estimation and proposed trade levels |
| [src/scanner.py](src/scanner.py) | Universe evaluation and candidate filtering |
| [src/risk.py](src/risk.py) | Shared position-sizing constraints |
| [src/portfolio.py](src/portfolio.py) | Position accounting and exit management |
| [src/broker.py](src/broker.py) | Internal simulation and Alpaca adapters |
| [src/ledger.py](src/ledger.py) | SQLite transaction records and schema migration |
| [src/backtest.py](src/backtest.py) | Historical simulation and performance metrics |
| [src/validation.py](src/validation.py) | Expanding-window parameter selection |
| [backtest.py](backtest.py) | Evaluation command-line interface |
| [server.py](server.py) | FastAPI dashboard for both ledgers |
| [close_all.py](close_all.py) | Explicit liquidation utility |

The internal simulator is the default backend. The Alpaca adapter supports
equity bracket orders and defaults to the paper endpoint. A non-paper endpoint
requires explicit configuration with `ALPACA_ALLOW_LIVE=1`. Cryptocurrency
execution uses the internal simulator.

Dashboard addresses: [equities](https://trader.marwansummakieh.me) and
[cryptocurrencies](https://trader.marwansummakieh.me/crypto).

## 5. Reproducibility and operation

### 5.1 Environment

Python 3.11 or later is required:

```bash
git clone https://github.com/MarwanSummakieh/trader.git
cd trader
python -m venv .venv
```

Activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell or
`source .venv/bin/activate` in a POSIX shell, then install dependencies:

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

Dependency files specify lower version bounds. Exact replication across machines
additionally requires recording the installed package versions.

### 5.2 Configuration and processes

Configuration is read from environment variables and an optional, untracked
`.env` file in the repository root. An internal simulation configuration is:

```dotenv
BROKER=paper
ENABLE_STOCKS=1
ENABLE_CRYPTO=0
STARTING_CAPITAL=10000
DB_PATH=ledger.db
POSITION_SIZE_PCT=0.15
RISK_PER_TRADE_PCT=0.005
MAX_PORTFOLIO_RISK_PCT=0.025
LEVERAGE=1.0
```

For Alpaca paper execution, set `BROKER=alpaca` and provide `ALPACA_API_KEY`
and `ALPACA_SECRET_KEY`. Credentials are excluded from source control and the
Docker build context. Setting an individual risk fraction to zero disables that
constraint.

Run the equity process and dashboard in separate activated terminals:

```bash
python main.py
python server.py
```

The local dashboard is available at [localhost:5000](http://localhost:5000).
Run an independent cryptocurrency process in a separate PowerShell session:

```powershell
$env:ENABLE_STOCKS = '0'
$env:ENABLE_CRYPTO = '1'
$env:BROKER = 'paper'
$env:DB_PATH = 'ledger-crypto.db'
$env:STARTING_CAPITAL = '1000'
$env:CRYPTO_MAX_CAPITAL_PCT = '1.0'
python main.py
```

These assignments persist for the lifetime of that shell. Use
`python main.py --scan-once` for a single scan and `python main.py --stats`
for ledger statistics. `python close_all.py` requests liquidation of open
positions.

### 5.3 Historical evaluation

Evaluate the current equity configuration or run parameter selection:

```bash
python backtest.py --stocks-only
python backtest.py --stocks-only --walk-forward --report reports/stocks.json
```

Reproduce the recorded equity experiment from an existing local snapshot:

```bash
python backtest.py --stocks-only --cache-date 20260903 --walk-forward --report reports/stocks-reproduced.json
```

For the cryptocurrency experiment, use a separate PowerShell session:

```powershell
$env:STARTING_CAPITAL = '1000'
$env:CRYPTO_MAX_CAPITAL_PCT = '1.0'
python backtest.py --crypto-only --cache-date 20260903 --walk-forward --report reports/crypto-reproduced.json
```

The cache is excluded from version control and must already exist for offline
reproduction. Pickle files should be loaded only from trusted sources. Parameter
values and coverage should be compared with the recorded JSON reports.
`--sweep` reports in-sample rankings; `--folds`, `--min-trades`, and
`--max-drawdown` configure chronological evaluation. The CLI otherwise follows
`ENABLE_STOCKS` and `ENABLE_CRYPTO`.

### 5.4 Verification

```bash
python -m pytest
```

The 2026-09-06 verification run passed 115 offline tests. Coverage includes entry
conditions, sizing, execution timing, fees, trailing stops, ledger migration,
chronological selection, and dashboard isolation. Passing these tests establishes
consistency with tested software contracts; it does not establish an economic
advantage.

### 5.5 Container deployment

```bash
docker compose up -d --build
```

The supplied Compose configuration defines equity and cryptocurrency processes,
a dashboard on port 5000, and a Cloudflare tunnel service. The tunnel requires
`CF_TUNNEL_TOKEN` when used. Each trading process uses a separate database in
the persistent `trader_data` volume. Container replacement preserves records
when that volume is retained. Registry-based deployment is documented in
[DEPLOY.md](DEPLOY.md).

## References

Bailey, D. H., Borwein, J. M., López de Prado, M., and Zhu, Q. J. (2017).
The probability of backtest overfitting. *Journal of Computational Finance*,
20(4), 39–69. [doi:10.21314/JCF.2016.322](https://doi.org/10.21314/JCF.2016.322).
[Open-access record](https://escholarship.org/uc/item/4w1110bb).
