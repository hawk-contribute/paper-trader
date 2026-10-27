"""Shared core: data fetch, indicators, strategies, simulated fills.
PAPER TRADING ONLY. No API keys, no order endpoints, no real money."""
import json, time, urllib.request

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
FEE = 0.001          # 0.1% per fill
SLIPPAGE = 0.0005    # 0.05% adverse slippage per fill
MAX_POS_FRAC = 0.33  # max 33% of equity per position
KILL_EQUITY = 70.0   # halt if equity < 70 USDT
START_EQUITY = 100.0
TARGET = 200.0

SOURCES = ["https://data-api.binance.vision", "https://api.binance.com"]

def _get(path):
    last = None
    for base in SOURCES:
        try:
            with urllib.request.urlopen(base + path, timeout=15) as r:
                d = json.loads(r.read())
                if isinstance(d, dict) and d.get("code") not in (None, 200):
                    raise RuntimeError(d)
                return d
        except Exception as e:
            last = e
    # fallback: OKX public candles
    raise RuntimeError(f"all sources failed: {last}")

def okx_klines(symbol, limit=300):
    inst = symbol.replace("USDT", "-USDT")
    with urllib.request.urlopen(f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar=1H&limit={min(limit,300)}", timeout=15) as r:
        d = json.loads(r.read())["data"]
    out = []
    for row in reversed(d):
        if row[8] != "1":  # unconfirmed candle
            continue
        t = int(row[0]); out.append(dict(t=t, o=float(row[1]), h=float(row[2]), l=float(row[3]), c=float(row[4]), ct=t+3600000-1))
    return out

def klines(symbol, start_ms=None, limit=1000, end_ms=None):
    """Hourly klines; returns only CLOSED candles."""
    out = []
    try:
        if start_ms is None:
            d = _get(f"/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}")
            out = [dict(t=k[0], o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]), ct=k[6]) for k in d]
        else:
            cur = start_ms
            end_ms = end_ms or int(time.time()*1000)
            while cur < end_ms:
                d = _get(f"/api/v3/klines?symbol={symbol}&interval=1h&startTime={cur}&limit=1000")
                if not d: break
                out += [dict(t=k[0], o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]), ct=k[6]) for k in d]
                cur = d[-1][0] + 3600000
                if len(d) < 1000: break
    except Exception:
        out = okx_klines(symbol, limit)
    now = int(time.time()*1000)
    return [k for k in out if k["ct"] < now]

# ---------------- indicators ----------------
def ema(vals, n):
    a = 2/(n+1); out = []; e = None
    for v in vals:
        e = v if e is None else a*v + (1-a)*e
        out.append(e)
    return out

def atr(bars, n=14):
    trs = []; prev = None
    for b in bars:
        tr = b["h"]-b["l"] if prev is None else max(b["h"]-b["l"], abs(b["h"]-prev), abs(b["l"]-prev))
        trs.append(tr); prev = b["c"]
    out = []; a = None
    for i, tr in enumerate(trs):  # Wilder smoothing
        a = tr if a is None else (a*(n-1)+tr)/n
        out.append(a)
    return out

def rsi(closes, n=14):
    out = [50.0]; ag = al = None
    for i in range(1, len(closes)):
        ch = closes[i]-closes[i-1]; g = max(ch, 0); l = max(-ch, 0)
        if ag is None: ag, al = g, l
        else: ag = (ag*(n-1)+g)/n; al = (al*(n-1)+l)/n
        out.append(100.0 if al == 0 else 100 - 100/(1+ag/al))
    return out

def sma_std(vals, n):
    m, s = [], []
    for i in range(len(vals)):
        w = vals[max(0, i-n+1):i+1]; mu = sum(w)/len(w)
        m.append(mu); s.append((sum((x-mu)**2 for x in w)/len(w))**0.5)
    return m, s

def indicators(bars):
    c = [b["c"] for b in bars]
    bm, bs = sma_std(c, 20)
    return dict(ema20=ema(c, 20), ema50=ema(c, 50), ema200=ema(c, 200), atr=atr(bars), rsi=rsi(c), bbm=bm, bbs=bs)

# ---------------- strategies ----------------
# Each returns (action, reason, extra) at bar index i (bar i is CLOSED; act at its close).
# Strategy A "trend_score": EMA20/50 trend-following, deterministic score 0-5 as the "AI" layer.
def trend_score_entry(bars, ind, i):
    c = bars[i]["c"]; s = 0; why = []
    if ind["ema20"][i] > ind["ema50"][i]: s += 1; why.append("EMA20>EMA50")
    if c > ind["ema200"][i]: s += 1; why.append("close>EMA200")
    if 50 <= ind["rsi"][i] <= 70: s += 1; why.append(f"RSI {ind['rsi'][i]:.0f} in 50-70")
    if i >= 24 and c > bars[i-24]["c"]: s += 1; why.append("24h momentum +")
    if ind["ema50"][i] > ind["ema50"][i-5]: s += 1; why.append("EMA50 rising")
    crossed = ind["ema20"][i] > ind["ema50"][i] and ind["ema20"][i-1] <= ind["ema50"][i-1]
    fresh = crossed or (s == 5 and ind["ema20"][i] > ind["ema50"][i])
    return s, why, (s >= 4 and fresh)

STRATS = {}
def strategy(name):
    def d(f): STRATS[name] = f; return f
    return d

@strategy("trend_score")
def strat_trend(bars, ind, i, pos):
    c = bars[i]["c"]; a = ind["atr"][i]
    if pos is None:
        s, why, go = trend_score_entry(bars, ind, i)
        if go:
            return "BUY", f"score {s}/5 ({', '.join(why)})", dict(stop=c-2.0*a, trail_mult=3.0)
        return "HOLD", f"no entry, score {s}/5", None
    # manage
    if ind["ema20"][i] < ind["ema50"][i]:
        return "SELL", "EMA20 crossed below EMA50 (trend over)", None
    new_trail = c - pos["trail_mult"]*a
    if new_trail > pos["stop"]:
        pos["stop"] = new_trail
    return "HOLD", f"in trend, stop {pos['stop']:.4f}", None

# Strategy B "meanrev": Bollinger/RSI dip-buy only when above EMA200 (buy dips in uptrend).
@strategy("meanrev")
def strat_meanrev(bars, ind, i, pos):
    c = bars[i]["c"]; a = ind["atr"][i]
    if pos is None:
        lower = ind["bbm"][i] - 2*ind["bbs"][i]
        if c < lower and ind["rsi"][i] < 35 and c > ind["ema200"][i]*0.97:
            return "BUY", f"close<{lower:.4f} lower BB, RSI {ind['rsi'][i]:.0f}<35, near/above EMA200", dict(stop=c-2.0*a, trail_mult=None, max_bars=48)
        return "HOLD", "no dip signal", None
    if c >= ind["bbm"][i]:
        return "SELL", "reverted to BB middle (take profit)", None
    if pos["bars"] >= pos["max_bars"]:
        return "SELL", "time stop 48h", None
    return "HOLD", f"waiting for reversion, stop {pos['stop']:.4f}", None

# ---------------- portfolio simulation ----------------
def new_state(strategy_name):
    return dict(strategy=strategy_name, cash=START_EQUITY, positions={}, trades=[], equity_history=[], halted=False, last_bar={})

def equity(state, prices):
    return state["cash"] + sum(p["qty"]*prices.get(s, p["entry"]) for s, p in state["positions"].items())

def _buy(state, sym, price, t, reason, extra, prices):
    eq = equity(state, prices)
    alloc = min(eq*MAX_POS_FRAC, state["cash"])
    if alloc < 5: return None
    fill = price*(1+SLIPPAGE); fee = alloc*FEE
    qty = (alloc-fee)/fill
    state["cash"] -= alloc
    pos = dict(qty=qty, entry=fill, cost=alloc, t=t, bars=0, stop=extra["stop"], trail_mult=extra.get("trail_mult"), max_bars=extra.get("max_bars"))
    state["positions"][sym] = pos
    tr = dict(t=t, sym=sym, side="BUY", price=fill, qty=qty, usdt=alloc, fee=fee, reason=reason)
    state["trades"].append(tr); return tr

def _sell(state, sym, price, t, reason):
    pos = state["positions"].pop(sym)
    fill = price*(1-SLIPPAGE); gross = pos["qty"]*fill; fee = gross*FEE
    state["cash"] += gross-fee
    pnl = gross-fee-pos["cost"]
    tr = dict(t=t, sym=sym, side="SELL", price=fill, qty=pos["qty"], usdt=gross-fee, fee=fee, pnl=pnl, pnl_pct=pnl/pos["cost"]*100, reason=reason)
    state["trades"].append(tr); return tr

def step_symbol(state, sym, bars, ind, i, prices, log):
    """Process closed bar i for sym. Stop-loss checked intrabar on the bar's low."""
    f = STRATS[state["strategy"]]; b = bars[i]; t = b["t"]
    pos = state["positions"].get(sym)
    if pos is not None:
        pos["bars"] += 1
        if b["l"] <= pos["stop"]:
            px = min(pos["stop"], b["o"])  # gap-down fills at open
            tr = _sell(state, sym, px, t, f"STOP-LOSS hit at {pos['stop']:.4f} (bar low {b['l']:.4f})")
            log(t, sym, "SELL", tr["reason"], tr); return
    if state["halted"]:
        log(t, sym, "HALTED", "kill-switch active", None); return
    action, reason, extra = f(bars, ind, i, state["positions"].get(sym))
    tr = None
    if action == "BUY":
        tr = _buy(state, sym, b["c"], t, reason, extra, prices)
        if tr is None: action, reason = "HOLD", reason + " | skipped: insufficient cash"
    elif action == "SELL":
        tr = _sell(state, sym, b["c"], t, reason)
    log(t, sym, action, reason, tr)

def check_kill(state, prices, t, log):
    eq = equity(state, prices)
    if not state["halted"] and eq < KILL_EQUITY:
        for sym in list(state["positions"]):
            tr = _sell(state, sym, prices[sym], t, f"KILL-SWITCH: equity {eq:.2f} < {KILL_EQUITY}")
            log(t, sym, "SELL", tr["reason"], tr)
        state["halted"] = True
    return equity(state, prices)

# Strategy C "trend_slow": slower trend-following to cut whipsaw/fees.
# Entry: EMA50 crosses above EMA200 (or EMA50>EMA200 and close makes a fresh 48h high) with RSI<75.
# Exit: EMA50 < EMA200, or ATR trailing stop (3x initial, 4x trail).
@strategy("trend_slow")
def strat_trend_slow(bars, ind, i, pos):
    c = bars[i]["c"]; a = ind["atr"][i]; e50, e200 = ind["ema50"], ind["ema200"]
    if pos is None:
        up = e50[i] > e200[i]
        cross = up and e50[i-1] <= e200[i-1]
        hi48 = max(b["h"] for b in bars[i-48:i])
        breakout = up and c > hi48
        if (cross or breakout) and ind["rsi"][i] < 75:
            why = "EMA50 crossed above EMA200" if cross else f"EMA50>EMA200 and close {c:.4f} > 48h high {hi48:.4f}"
            return "BUY", f"{why}, RSI {ind['rsi'][i]:.0f}<75", dict(stop=c-3.0*a, trail_mult=4.0)
        return "HOLD", "no uptrend breakout" if up else "EMA50<=EMA200 (downtrend), stay flat", None
    if e50[i] < e200[i]:
        return "SELL", "EMA50 crossed below EMA200 (trend over)", None
    nt = c - pos["trail_mult"]*a
    if nt > pos["stop"]: pos["stop"] = nt
    return "HOLD", f"in trend, trailing stop {pos['stop']:.4f}", None
