# paper-trader（紙上交易 / paper trading only）

**📊 即時狀態：[STATUS.md](STATUS.md)**（每次執行自動更新，繁體中文，UTC+8）

Simulation only: no exchange API keys, no order endpoints, no real money. Started with a simulated 100 USDT.
GitHub Actions runs `tick.py` about every 5 minutes (`.github/workflows/tick.yml`), does exactly one check,
and commits `state.json`, `decisions.csv`, `tick.log`, `STATUS.md` back to the repo.

## Strategy (trend_aggr 1h_base, see `aggr.py`, `config.json`)
- Universe BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT (spot, long-only, at most one coin, ~98% of equity).
- Entry when flat: EMA20 > EMA50, close > EMA20, 24h momentum > 0, close above prior 24h high, and RSI(14) strictly between 50 and 75
  (all on 1h klines with the forming bar updated by the live price). Strongest momentum wins.
- Exit: ATR stop (2x ATR initial, 3x ATR trailing, ratchets up only) or EMA20 < EMA50. 15-min cooldown per coin after an exit.
- Because ticks are ~5 min apart, each tick also scans the held coin's 5m klines since the previous tick; if any 5m low touched
  the stop, the position is sold at the stop price (or at that 5m bar's open if it gapped below). Toggle: `intrabar_stop_check_5m`.
- Fills: 0.1% fee + 0.05% adverse slippage. Kill-switch: equity < 70 USDT -> sell all, halt permanently.
- No daily profit target.

## Data sources (public, keyless)
data-api.binance.vision -> api.binance.com -> api.binance.us -> OKX public market API (fallback chain). The source used is shown in STATUS.md.

## Files
`tick.py` (one check), `core.py` / `aggr.py` (indicators, strategy, simulated fills), `state.json` (account), `decisions.csv`
(decisions on change/trade, capped ~1.5 MB), `tick.log` (last 3000 lines), `STATUS.md` (human status).
Migrated from the original box loop on 2026-10-27 (UTC+8).

## Activation pending
The workflow file is kept at `workflow-template/tick.yml` because the pushing token lacks GitHub's `workflow` scope.
To activate: copy it to `.github/workflows/tick.yml` (GitHub web: Add file -> Create new file) and re-sync `state.json` from the box first.
