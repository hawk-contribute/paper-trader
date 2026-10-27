"""ONE paper-trading check per invocation (run by GitHub Actions every ~5 min).
PAPER ONLY: no API keys, no order endpoints, simulated fills (0.1% fee + 0.05% slippage).
Logic = box live_rt.py trend_aggr branch: 1h_base, RSI(14) 50-75 entry filter on forming bar + live price,
2xATR initial stop, 3xATR trailing, one coin ~98% equity, 15-min cooldown, 70 USDT kill-switch (sell all, halt).
Extra (because ticks are ~5 min apart): before the normal stop check, the held coin's 5m klines since the
previous tick are scanned; if any 5m low <= stop, the position is sold at the stop (or at the 5m open if it gapped below)."""
import json, os, csv, time, re, urllib.request, urllib.parse
import core, aggr

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json"); CSV = os.path.join(HERE, "decisions.csv")
LOG = os.path.join(HERE, "tick.log"); STATUS = os.path.join(HERE, "STATUS.md")
CFG = json.load(open(os.path.join(HERE, "config.json")))
P = aggr.VARIANTS[CFG.get("variant", "1h_base")]
SYMS = aggr.UNIVERSE
TZ8 = 8*3600
LOG_MAX_LINES = 3000; CSV_MAX_BYTES = 1_500_000; EQ_HIST_MAX = 2016   # ~7 days at 5 min

def ts(sec): return time.strftime("%Y-%m-%d %H:%M:%S UTC+0800", time.gmtime(sec + TZ8))
def day8(sec): return time.strftime("%Y-%m-%d", time.gmtime(sec + TZ8))

def get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-trader/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ---- data sources (public, keyless) with fallback ----
BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com", "https://api.binance.us"]

def _binance(path):
    last = None
    for base in BINANCE_BASES:
        for attempt in range(2):
            try:
                d = get(base + path)
                if isinstance(d, dict) and "code" in d and d.get("code") not in (0, 200):
                    raise RuntimeError(str(d)[:200])
                return d, base
            except Exception as e:
                last = f"{base}: {e!r}"; time.sleep(1)
    raise RuntimeError(f"all Binance-format sources failed: {last}")

def okx_inst(s): return s.replace("USDT", "-USDT")

def fetch_prices():
    try:
        q = urllib.parse.quote(json.dumps(SYMS, separators=(",", ":")))
        d, base = _binance("/api/v3/ticker/price?symbols=" + q)
        return {x["symbol"]: float(x["price"]) for x in d}, base
    except Exception as e:
        out = {}
        for s in SYMS:
            d = get(f"https://www.okx.com/api/v5/market/ticker?instId={okx_inst(s)}")
            out[s] = float(d["data"][0]["last"])
        return out, "https://www.okx.com"

def fetch_klines(sym, interval, limit=400, start_ms=None):
    """Returns (closed, forming) in Binance-like dicts."""
    now = time.time()*1000
    try:
        path = f"/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}" + (f"&startTime={start_ms}" if start_ms else "")
        d, base = _binance(path)
        bars = [dict(t=k[0], o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]), ct=k[6]) for k in d]
    except Exception:
        bar = {"1h": "1H", "5m": "5m"}[interval]; step = {"1h": 3600000, "5m": 300000}[interval]
        rows = []; after = None
        while len(rows) < limit:   # OKX: max 300 per page, newest first
            url = f"https://www.okx.com/api/v5/market/candles?instId={okx_inst(sym)}&bar={bar}&limit=300" + (f"&after={after}" if after else "")
            d = get(url)["data"]
            if not d: break
            rows += d; after = d[-1][0]
            if start_ms and int(after) <= start_ms: break
        bars = sorted([dict(t=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), ct=int(r[0])+step-1) for r in rows], key=lambda b: b["t"])
        if start_ms: bars = [b for b in bars if b["ct"] >= start_ms]
        bars = bars[-limit:]
    closed = [b for b in bars if b["ct"] < now]
    forming = bars[-1] if bars and bars[-1]["ct"] >= now else None
    return closed, forming

# ---- logging ----
def log_line(msg):
    lines = open(LOG).read().splitlines() if os.path.exists(LOG) else []
    lines.append(f"{ts(time.time())} {msg}")
    open(LOG, "w").write("\n".join(lines[-LOG_MAX_LINES:]) + "\n")

def csv_row(row):
    if os.path.exists(CSV) and os.path.getsize(CSV) > CSV_MAX_BYTES:   # keep newest ~half
        lines = open(CSV).read().splitlines(); hdr, body = lines[0], lines[1:]
        open(CSV, "w").write("\n".join([hdr] + body[len(body)//2:]) + "\n")
    new = not os.path.exists(CSV)
    with open(CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new: w.writerow(["run_time", "bar_time", "symbol", "action", "reason", "fill_price", "qty", "pnl_usdt"])
        w.writerow(row)

def main():
    t0 = time.time(); tms = int(t0*1000)
    st = json.load(open(STATE))
    st.setdefault("cooldown_until", {}); st.setdefault("last_key", {})
    st["strategy"] = "trend_aggr"; st["mode"] = "GitHub Actions, every ~5 min"
    events = []
    def record(sym, action, reason, tr, bar_t):
        key = action + re.sub(r"[\d.]+", "#", reason)
        if tr or st["last_key"].get(sym) != key:
            csv_row([ts(time.time()), ts(bar_t/1000), sym, action, reason,
                     f"{tr['price']:.6f}" if tr else "", f"{tr['qty']:.8f}" if tr else "",
                     f"{tr['pnl']:.4f}" if tr and tr["side"] == "SELL" else ""])
            events.append(f"{sym} {action}: {reason}")
            st["last_key"][sym] = key
    prices, src = fetch_prices()
    prev_tick_ms = st.get("last_tick_ms") or (tms - 300000)

    if not st["halted"]:
        snap = {}
        for s in SYMS:
            closed, forming = fetch_klines(s, "1h"); px = prices[s]
            hour = tms // 3600000 * 3600000
            fb = dict(forming) if forming else dict(t=hour, o=px, h=px, l=px, c=px, ct=hour+3599999)
            fb["h"] = max(fb["h"], px); fb["l"] = min(fb["l"], px); fb["c"] = px
            bars = closed + [fb]; snap[s] = (bars, aggr.compute(bars, P), len(bars)-1)
        for s in list(st["positions"]):
            bars, ind, i = snap[s]; pos = st["positions"][s]; px = prices[s]
            if pos.get("stop", 0) <= 0 or not pos.get("trail_mult"):
                pos["stop"] = px - P["trail_atr"]*ind["atr"][i]; pos["trail_mult"] = P["trail_atr"]
            # (a) stop hit between ticks? scan 5m lows since previous tick (before ratcheting the stop)
            if CFG.get("intrabar_stop_check_5m", True):
                c5, f5 = fetch_klines(s, "5m", limit=100, start_ms=max(prev_tick_ms - 300000, pos["t"]))
                hit = next((b for b in c5 + ([f5] if f5 else []) if b["ct"] >= prev_tick_ms and b["t"] >= pos["t"] // 300000 * 300000 and b["l"] <= pos["stop"]), None)
                if hit:
                    fill = min(pos["stop"], hit["o"])
                    tr = core._sell(st, s, fill, tms, f"ATR STOP hit between ticks: 5m bar {ts(hit['t']/1000)} low {hit['l']:.4f} <= stop {pos['stop']:.4f}")
                    st["cooldown_until"][s] = t0 + CFG.get("cooldown_sec", 900); record(s, "SELL", tr["reason"], tr, bars[i]["t"]); continue
            # (b) stop vs latest price
            if px <= pos["stop"]:
                tr = core._sell(st, s, px, tms, f"TRAILING/ATR STOP: live {px:.4f} <= stop {pos['stop']:.4f}")
                st["cooldown_until"][s] = t0 + CFG.get("cooldown_sec", 900); record(s, "SELL", tr["reason"], tr, bars[i]["t"]); continue
            ex, why = aggr.exit_signal(bars, ind, i, P, pos)
            if ex:
                tr = core._sell(st, s, px, tms, why + " [forming bar + live price]")
                st["cooldown_until"][s] = t0 + CFG.get("cooldown_sec", 900); record(s, "SELL", tr["reason"], tr, bars[i]["t"])
            else:
                record(s, "HOLD", why, None, bars[i]["t"])
        if not st["positions"]:
            rf = CFG.get("rsi_filter") or {}; per = int(rf.get("period", 14))
            rsi_now = {s: core.rsi([x["c"] for x in snap[s][0]], per)[snap[s][2]] for s in snap}
            lo, hi = float(rf.get("min", 50)), float(rf.get("max", 75))
            passed = {s: v for s, v in snap.items() if (not rf.get("enabled")) or lo < rsi_now[s] < hi}
            best, notes = aggr.pick(passed, P, lambda s: t0 < st["cooldown_until"].get(s, 0)) if passed else (None, {})
            for s in snap:
                notes[s] = f"RSI{per} {rsi_now[s]:.1f} outside ({rf.get('min', 50)}, {rf.get('max', 75)})" if s not in passed else notes.get(s, "") + f", RSI {rsi_now[s]:.1f} ok"
            st["scan"] = {s: dict(rsi=round(rsi_now[s], 2), note=notes[s], time=ts(t0), price=prices[s]) for s in snap}
            if best:
                sym = best[0]; bars, ind, i = snap[sym]
                tr = aggr.buy_all(st, sym, prices[sym], tms, f"strongest uptrend: {best[2]}, RSI {rsi_now[sym]:.1f} in filter range [forming bar + live price]", P, ind["atr"][i], prices)
                if tr: record(sym, "BUY", tr["reason"], tr, bars[i]["t"])
            else:
                record("SCAN", "FLAT", "no eligible uptrend: " + "; ".join(f"{k}: {v}" for k, v in notes.items()), None, tms)
        else:
            per = int((CFG.get("rsi_filter") or {}).get("period", 14))
            st["scan"] = {s: dict(rsi=round(core.rsi([x["c"] for x in snap[s][0]], per)[snap[s][2]], 2),
                                  note=("holding" if s in st["positions"] else "not evaluated while holding"), time=ts(t0), price=prices[s]) for s in snap}
        eq = core.check_kill(st, prices, tms, lambda t, sym, a, r, tr: record(sym, a, r, tr, t))
    else:
        eq = core.equity(st, prices); record("PORTFOLIO", "HALTED", "kill-switch active; no trading", None, tms)

    st["equity_history"].append(dict(run=ts(t0), bar="actions", equity=round(eq, 4), prices=prices))
    st["equity_history"] = st["equity_history"][-EQ_HIST_MAX:]
    st["ticks"] = st.get("ticks", 0) + 1; st["last_prices"] = prices; st["last_equity"] = round(eq, 4)
    st["updated"] = ts(t0); st["last_tick_ms"] = tms; st["price_source"] = src
    tmp = STATE + ".tmp"; json.dump(st, open(tmp, "w"), indent=1); os.replace(tmp, STATE)
    log_line(f"tick equity {eq:.4f} cash {st['cash']:.4f} pos {list(st['positions'])} src {src} " + (" | " + " | ".join(events) if events else ""))
    write_status(st, eq, prices)
    print(f"[{ts(t0)}] equity {eq:.4f} src {src}; " + "; ".join(events))

ZH = [("EMA20<=EMA50", "EMA20 未高於 EMA50"), ("close<=EMA20", "收盤未站上 EMA20"), ("mom24<=0", "24 小時動能 ≤ 0"), ("no 24-bar breakout", "未突破前 24 小時高點")]
def zh(note):
    if note == "holding": return "持有中"
    if note.startswith("not evaluated"): return "持倉期間不評估"
    m = re.match(r"RSI(\d+) ([\d.]+) outside \((\S+), (\S+)\)", note)
    if m: return f"RSI {m.group(2)} 不在 {m.group(3)}–{m.group(4)} 區間"
    if note.startswith("eligible"): return "符合進場條件"
    if note.startswith("cooldown"): return "冷卻中"
    parts = [v for k, v in ZH if k in note]
    return ("、".join(parts) if parts else note) + ("（RSI 合格）" if "ok" in note else "")

def write_status(st, eq, prices):
    L = ["# 紙上交易狀態（模擬，不用真錢、不下單）", "",
         f"最後更新：{st['updated'].replace(' UTC+0800', '')}（UTC+8）｜由 GitHub Actions 約每 5 分鐘執行一次｜價格來源 {st.get('price_source') or '主機 live_rt.py（遷移前快照）'}", "",
         "| 項目 | 數值 |", "|---|---|",
         f"| 權益 | {eq:.2f} USDT |", f"| 相對起始 100 | {(eq/100-1)*100:+.2f}% |",
         f"| 距 200 USDT 目標進度 | {(eq-100)/100*100:.1f}% |", f"| 現金 | {st['cash']:.2f} USDT |",
         f"| 熔斷（低於 70 USDT） | {'已觸發，停止交易' if st['halted'] else '未觸發'} |", ""]
    L += ["## 持倉", ""]
    if st["positions"]:
        L += ["| 幣種 | 數量 | 進場價 | 現價 | 市值 | 損益 | 停損價 |", "|---|---:|---:|---:|---:|---:|---:|"]
        for s, p in st["positions"].items():
            v = p["qty"]*prices[s]
            L.append(f"| {s} | {p['qty']:.6f} | {p['entry']:.4f} | {prices[s]:.4f} | {v:.2f} | {(v/p['cost']-1)*100:+.2f}% | {p['stop']:.4f}（{(p['stop']/prices[s]-1)*100:+.2f}%） |")
    else:
        L.append("目前空手（全部現金）。")
    L += ["", "## 最新掃描", "", "| 幣種 | 價格 | RSI(14) | 說明 |", "|---|---:|---:|---|"]
    for s, v in (st.get("scan") or {}).items():
        L.append(f"| {s} | {v.get('price', prices.get(s, 0)):,.4f} | {v['rsi']:.1f} | {zh(v['note'])} |")
    L += ["", "## 最近交易", "", "| 時間（UTC+8） | 幣種 | 方向 | 成交價 | 金額 USDT | 損益 USDT |", "|---|---|---|---:|---:|---:|"]
    for x in st["trades"][-8:][::-1]:
        L.append(f"| {time.strftime('%Y-%m-%d %H:%M', time.gmtime(x['t']/1000 + TZ8))} | {x['sym']} | {'買入' if x['side']=='BUY' else '賣出'} | {x['price']:.4f} | {x['usdt']:.2f} | {x['pnl']:+.2f} |" if 'pnl' in x else
                 f"| {time.strftime('%Y-%m-%d %H:%M', time.gmtime(x['t']/1000 + TZ8))} | {x['sym']} | {'買入' if x['side']=='BUY' else '賣出'} | {x['price']:.4f} | {x['usdt']:.2f} | |")
    L += ["", "策略：trend_aggr 1h_base（EMA20/50、24 小時動能與突破、RSI 50–75 濾網、2×ATR 初始停損、3×ATR 追蹤停損、單一幣種約 98% 權益、出場後冷卻 15 分鐘、每筆 0.1% 手續費 + 0.05% 滑價）。", ""]
    open(STATUS, "w").write("\n".join(L))

if __name__ == "__main__":
    main()
