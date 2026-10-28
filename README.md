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
- 分批停利（partial take-profit）：持倉價格達進場價 +5% 時先賣出一半（每個持倉只觸發一次，`tp1_done`）。即時價格達標以即時價成交；兩次執行之間若 5 分鐘 K 線最高價達標，以目標價成交（若開盤即跳空高於目標，以該根開盤價成交）。同一根 5 分鐘 K 線同時碰到停損與停利時，保守假設先停損。剩下一半沿用原本出場規則（2×ATR 初始／3×ATR 追蹤停損、EMA20 跌破 EMA50、70 USDT 熔斷），停損不上移到成本價。成本依比例分攤計算損益；冷卻 15 分鐘只在全部出場後開始。設定：`config.json` 的 `partial_take_profit`。
  (Partial TP: at entry +5% sell 50% once per position; rest keeps existing exits; cost basis split proportionally.)
- Fills: 0.1% fee + 0.05% adverse slippage. Kill-switch: equity < 70 USDT -> sell all, halt permanently.
- No daily profit target.

## Data sources (public, keyless)
data-api.binance.vision -> api.binance.com -> api.binance.us -> OKX public market API (fallback chain). The source used is shown in STATUS.md.

## Files
`tick.py` (one check), `core.py` / `aggr.py` (indicators, strategy, simulated fills), `state.json` (account), `decisions.csv`
(decisions on change/trade, capped ~1.5 MB), `tick.log` (last 3000 lines), `STATUS.md` (human status).
Migrated from the original box loop on 2026-10-27 (UTC+8).

