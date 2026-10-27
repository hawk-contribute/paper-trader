"""trend_aggr: concentrated momentum trend-following (spot, long-only, max ONE coin, ~98% of equity).
Shared by backtest_aggr.py and live_rt.py. PAPER ONLY."""
import core

UNIVERSE = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
VARIANTS = {
 # name: timeframe, EMA fast/slow, momentum lookback (bars), breakout lookback (0=off), initial stop ATR, trail ATR, extra exit on mom<0, size
 "1h_base":  dict(tf="1h",  ef=20, es=50, mom_n=24, brk=24, stop_atr=2.0, trail_atr=3.0, mom_exit=False, size=0.98, cd_bars=1),
 "1h_tight": dict(tf="1h",  ef=20, es=50, mom_n=24, brk=0,  stop_atr=1.5, trail_atr=2.0, mom_exit=True,  size=0.98, cd_bars=1),
 "1h_fast":  dict(tf="1h",  ef=10, es=30, mom_n=12, brk=12, stop_atr=2.0, trail_atr=2.5, mom_exit=False, size=0.98, cd_bars=1),
 "15m_base": dict(tf="15m", ef=20, es=50, mom_n=24, brk=24, stop_atr=2.0, trail_atr=3.0, mom_exit=False, size=0.98, cd_bars=1),
 "15m_tight":dict(tf="15m", ef=20, es=50, mom_n=24, brk=0,  stop_atr=1.5, trail_atr=2.0, mom_exit=True,  size=0.98, cd_bars=1),
 "15m_fast": dict(tf="15m", ef=10, es=30, mom_n=12, brk=12, stop_atr=2.0, trail_atr=2.5, mom_exit=False, size=0.98, cd_bars=1),
}

def compute(bars, p):
    c = [b["c"] for b in bars]
    ind = dict(ef=core.ema(c, p["ef"]), es=core.ema(c, p["es"]), atr=core.atr(bars))
    n = p["mom_n"]; L = p["brk"]
    ind["mom"] = [c[i]/c[i-n]-1 if i >= n else 0.0 for i in range(len(c))]
    ind["hh"] = [max(b["h"] for b in bars[i-L:i]) if L and i >= L else 0.0 for i in range(len(c))]
    return ind

def entry_signal(bars, ind, i, p):
    c = bars[i]["c"]; ef, es, mom, hh = ind["ef"][i], ind["es"][i], ind["mom"][i], ind["hh"][i]
    ok = ef > es and c > ef and mom > 0 and (p["brk"] == 0 or c > hh)
    why = f"EMA{p['ef']}>{p['es']}, close>EMA{p['ef']}, mom{p['mom_n']} {mom*100:+.2f}%" + (f", close>{p['brk']}-bar high {hh:.4f}" if p["brk"] else "")
    return ok, mom, why

def exit_signal(bars, ind, i, p, pos):
    """Returns (exit?, reason). Also ratchets trailing stop."""
    c = bars[i]["c"]; a = ind["atr"][i]
    if pos.get("stop", 0) <= 0:   # inherited position without a stop (e.g. from btc_hold)
        pos["stop"] = c - p["trail_atr"]*a
    if ind["ef"][i] < ind["es"][i]:
        return True, f"momentum loss: EMA{p['ef']}<EMA{p['es']}"
    if p["mom_exit"] and ind["mom"][i] < 0:
        return True, f"momentum loss: mom{p['mom_n']} {ind['mom'][i]*100:+.2f}% < 0"
    nt = c - p["trail_atr"]*a
    if nt > pos["stop"]: pos["stop"] = nt
    return False, f"trend intact, trailing stop {pos['stop']:.4f}"

def buy_all(st, sym, px, t, reason, p, atr_val, prices):
    eq = core.equity(st, prices); alloc = min(st["cash"], eq*p["size"])
    if alloc < 5: return None
    fill = px*(1+core.SLIPPAGE); fee = alloc*core.FEE; qty = (alloc-fee)/fill
    st["cash"] -= alloc
    st["positions"][sym] = dict(qty=qty, entry=fill, cost=alloc, t=t, bars=0, stop=px - p["stop_atr"]*atr_val, trail_mult=p["trail_atr"], max_bars=None)
    tr = dict(t=t, sym=sym, side="BUY", price=fill, qty=qty, usdt=alloc, fee=fee, reason=reason)
    st["trades"].append(tr); return tr

def pick(snap, p, in_cooldown):
    """snap: {sym: (bars, ind, i)}. Returns (sym, mom, why) of strongest eligible uptrend or None, plus per-symbol notes."""
    best = None; notes = {}
    for s, (bars, ind, i) in snap.items():
        ok, mom, why = entry_signal(bars, ind, i, p)
        if ok and in_cooldown(s): notes[s] = "cooldown"; continue
        if ok: notes[s] = "eligible"
        else:
            c = bars[i]["c"]; f = []
            if not ind["ef"][i] > ind["es"][i]: f.append(f"EMA{p['ef']}<=EMA{p['es']}")
            if not c > ind["ef"][i]: f.append(f"close<=EMA{p['ef']}")
            if not ind["mom"][i] > 0: f.append(f"mom{p['mom_n']}<=0")
            if p["brk"] and not c > ind["hh"][i]: f.append(f"no {p['brk']}-bar breakout")
            notes[s] = "not eligible (" + " & ".join(f) + ")"
        if ok and (best is None or mom > best[1]): best = (s, mom, why)
    return best, notes
