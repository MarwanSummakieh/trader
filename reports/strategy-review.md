# Chronological Strategy Evaluation: 2026-09-06

This experiment compares expanding-window parameter selection with fixed entry rules for equity momentum and cryptocurrency breakouts. The observed results do not establish an improvement from parameter selection. The evaluation used historical simulation; it did not submit broker orders.

## Walk-forward results

Local data snapshot: 2026-09-03. The first half of dates trains the initial choice, followed by three non-overlapping test blocks. Each fold selects only from earlier data. It requires at least 20 training trades, positive net PnL, and no more than 10% training drawdown. With no qualifying candidate it holds cash. All paths close at fold boundaries and compound capital.

| Market / path | Test trades | Test return | Maximum drawdown |
|---|---:|---:|---:|
| stocks / existing rule + risk caps | 1 | -0.35% | -0.66% |
| stocks / existing rule + fixed sizing | 1 | -0.35% | -0.66% |
| stocks / train-selected parameters / cash | 0 | +0.00% | 0.00% |
| crypto / existing rule + risk caps | 132 | +13.93% | -12.01% |
| crypto / existing rule + fixed sizing | 132 | +13.93% | -12.01% |
| crypto / train-selected parameters / cash | 42 | -0.98% | -9.37% |

Stocks: all three selection folds stayed in cash; one baseline test trade is insufficient evidence. Crypto: the selector stayed in cash for two folds and underperformed the existing rule overall. The observed results provide no empirical basis for replacing the existing entry rules with this selection procedure. The risk caps did not bind on these baseline test trades, so this snapshot shows no performance difference between capped and fixed sizing. Separate regression tests verify that wider stops and greater concurrent exposure trigger the caps.

## Data and assumptions

- stocks: 61 symbols; source bars 2026-06-11 09:30:00-04:00 through 2026-09-03 13:50:00-04:00; no missing requested symbols. Starting capital $10,000. Candidates: 9.
- crypto: 13 symbols; source bars 2026-07-06 20:00:00-04:00 through 2026-09-03 13:25:00-04:00; no missing requested symbols. Starting capital $1,000. Candidates: 3.
- Both instances: 1x leverage, 15% maximum committed capital per position, 0.1% slippage per side; new modeled loss caps are 0.5% per trade and 2.5% across open positions. Gaps and actual fills can exceed modeled stop risk.
- Regulatory costs use the existing configured SEC/TAF assumptions in the JSON files, not a date-specific fee schedule. Penny rounding, account-specific charges, crypto venue fees, funding, and market impact are omitted. The estimates are conditional on the simulation model and have not been reconciled against broker executions. [Alpaca regulatory fees](https://docs.alpaca.markets/us/docs/regulatory-fees) and [crypto fees](https://docs.alpaca.markets/us/docs/crypto-fees) describe venue-specific costs to check before deployment.
- The source window includes a partial final trading session. The static universe has survivorship bias. Historical earnings exclusions are not available in these OHLCV snapshots and are not replayed.
- Historical data were previously used for research; chronological separation here does not constitute an independent, previously unexamined holdout. Short samples, repeated strategy searches, and changing regimes limit inference. [Backtest overfitting research](https://papers.ssrn.com/sol3/Papers.cfm?abstract_id=2326253).
- The sample does not establish prospective profitability or the superiority of an alternative entry rule.

## Reproduction protocol

Run from the repository with its Python environment and the same trusted local cache. Reports contain candidate, risk, fee, signal, and data-coverage metadata. These commands do not contact a broker.

```powershell
.venv\Scripts\python.exe backtest.py --stocks-only --cache-date 20260903 --walk-forward --report reports/stocks-walk-forward-20260903.json
$env:STARTING_CAPITAL='1000'
$env:CRYPTO_MAX_CAPITAL_PCT='1.0'
.venv\Scripts\python.exe backtest.py --crypto-only --cache-date 20260903 --walk-forward --report reports/crypto-walk-forward-20260903.json
```

The crypto environment overrides apply only to that shell. For exact reproduction, use the remaining parameter values recorded in each report.

## Software verification

115 offline tests pass, covering both existing functionality and new regressions for risk sizing, live/sim sizing agreement, entry-bar stops, cash timing, gap handling, fees, daily-context causality, chronological selection, and legacy-ledger migration. Evaluation commands operated on cached market data independently of production ledgers.
