# Trader

An automated intraday momentum trading bot for US equities, executing through
[Alpaca](https://alpaca.markets) paper trading with server-side bracket
orders, a live web dashboard, and a backtest engine that every strategy rule
was validated against.

**🔴 Live:** [trader.marwansummakieh.me](https://trader.marwansummakieh.me)

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
- no entries after 12:00 ET, no entries on earnings reaction days

Exits are ATR-anchored stops with a 3R take-profit and an R-based trailing
stop, all resting **server-side at the broker** as a bracket order — so
stops fire even if the bot process dies. All stock positions are force-flat
at 15:45 ET; nothing is held overnight.

Costs are modeled honestly: per-side spread/slippage plus Alpaca's sell-side
regulatory fees (SEC + FINRA TAF — Alpaca charges no commission). The rule
set was validated on a train/holdout split with walk-forward checks
(~+0.14R/trade net of all costs); the full research notes live in
[config.py](config.py) and [CHANGELOG.md](CHANGELOG.md).

| Component | Role |
|---|---|
| `main.py` | the trading bot (scanner → entries → exit management) |
| `server.py` | FastAPI dashboard — positions, scans, orders, daily P&L |
| `src/broker.py` | execution layer: internal paper simulator or Alpaca bracket orders |
| `src/ledger.py` | SQLite trade ledger — the single source of truth |
| `backtest.py` | backtest / parameter-sweep CLI over 5-minute bars |
| `close_all.py` | wind down all open ledger trades (use before switching brokers) |

## Run it locally

Requires **Python 3.11+**.

```bash
git clone https://github.com/MarwanSummakieh/trader.git
cd trader

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### Create your `.env`

Create a file named `.env` in the project root (it's gitignored — your keys
never leave your machine and are never baked into any Docker image):

```bash
# --- broker ---------------------------------------------------------------
# "paper"  = internal simulator, no keys needed (default if omitted)
# "alpaca" = real order flow against Alpaca's free paper-trading API
BROKER=alpaca
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...

# --- optional -------------------------------------------------------------
STARTING_CAPITAL=10000        # ledger starting capital (default 10000)
# CF_TUNNEL_TOKEN=...         # only for the Cloudflare tunnel in docker-compose
```

Get the Alpaca keys from a free account at
[app.alpaca.markets](https://app.alpaca.markets): switch the dashboard to
**Paper** and generate an API key — it gives you both the key and the
secret. No funding or ID verification is needed for paper trading.

**No keys at all?** Leave `BROKER` unset and the bot runs against its
internal fill simulator — same strategy, simulated fills, zero setup.

The live endpoint is refused unless you *also* set `ALPACA_ALLOW_LIVE=1` —
the default configuration cannot touch real money.

### Start it

```bash
python main.py        # the bot (terminal 1)
python server.py      # the dashboard (terminal 2) → http://localhost:5000
```

Useful one-shots:

```bash
python main.py --scan-once    # run one market scan, print top setups, exit
python main.py --stats        # ledger statistics
python backtest.py            # backtest current strategy params (59d of 5m bars)
python backtest.py --sweep    # parameter grid search
python close_all.py           # close every open ledger trade at market
```

## Run it with Docker

```bash
docker compose up -d --build
```

This starts the bot, the dashboard on port 5000, and (if `CF_TUNNEL_TOKEN`
is set) a Cloudflare tunnel — which is how the live instance at
[trader.marwansummakieh.me](https://trader.marwansummakieh.me) is served
from a NAS. The `.env` file sits next to `docker-compose.yml` and is read at
container start.

The ledger lives in the `trader_data` volume, so it survives rebuilds.
Remember that code is baked into the image at build time — updating requires
the `--build` flag, not just a restart. For pulling a prebuilt image instead
of building locally (ideal for NAS deployments), see [DEPLOY.md](DEPLOY.md).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

60 tests cover the exit/trailing/sizing logic, every entry-rule clause, the
backtest simulator's fidelity rules, the fee model, and the broker layer
(with a stubbed Alpaca transport — no network in tests).
