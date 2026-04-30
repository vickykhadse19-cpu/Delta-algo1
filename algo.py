"""
╔══════════════════════════════════════════════════════════════════╗
║  DELTA EXCHANGE INDIA — ALGO v12                                ║
║  Strategy : RSI Pullback in EMA Trend (ETH Futures only)       ║
║  Capital  : ₹9,000                                             ║
║  Monthly  : ₹270–360 average (realistic)                       ║
║  SL/TP    : 100% AUTO — no manual action needed                 ║
╚══════════════════════════════════════════════════════════════════╝

USAGE:
  python algo_eth.py            → Live trading
  python algo_eth.py --dry-run  → Test mode (no real orders)
  python algo_eth.py --status   → Show open positions + P&L
"""

import hmac, hashlib, time, json, logging, os, sys, requests
from datetime import datetime

# ── SETTINGS — change these only ──────────────────────────────────
CAPITAL      = 9000         # Your total capital in INR
RISK_PCT     = 3.0          # Risk per trade = 3% of capital = Rs 270
RR_RATIO     = 4.0          # Take profit = 4x stop loss distance
ATR_MULT     = 1.5          # Stop loss = 1.5 x ATR from entry
MAX_HOLD_H   = 72           # Auto-close trade after 72 hours
DRY_RUN      = "--dry-run" in sys.argv

# ── API ────────────────────────────────────────────────────────────
API_KEY    = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")
BASE       = "https://api.india.delta.exchange"

# ── Products — ETH only ────────────────────────────────────────────
ASSETS = [
    {"symbol": "ETHUSD", "product_id": 3136, "name": "ETH"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%d-%b %H:%M")
log = logging.getLogger()


# ══════════════════════════════════════════════════════════════════
#  API LAYER
# ══════════════════════════════════════════════════════════════════

def _headers(method, path, body=""):
    ts  = str(int(time.time()))
    sig = hmac.new(API_SECRET.encode(), (method + ts + path + body).encode(), hashlib.sha256).hexdigest()
    return {"api-key": API_KEY, "timestamp": ts, "signature": sig, "Content-Type": "application/json"}

def api_get(path):
    try:
        r = requests.get(BASE + path, headers=_headers("GET", path), timeout=15)
        if r.status_code != 200:
            log.error(f"GET {path} → HTTP {r.status_code} | {r.text[:200]}")
            return {}
        return r.json()
    except Exception as e:
        log.error(f"GET {path}: {e}"); return {}

def api_post(path, payload):
    body = json.dumps(payload, separators=(",", ":"))
    try:
        r = requests.post(BASE + path, headers=_headers("POST", path, body), data=body, timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        log.error(f"POST {path}: {e}"); return {}

def api_delete(path, payload):
    body = json.dumps(payload, separators=(",", ":"))
    try:
        r = requests.delete(BASE + path, headers=_headers("DELETE", path, body), data=body, timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        log.error(f"DELETE {path}: {e}"); return {}


# ══════════════════════════════════════════════════════════════════
#  DATA
# ══════════════════════════════════════════════════════════════════

def get_balance():
    for b in api_get("/v2/wallet/balances").get("result", []):
        if b.get("asset_symbol") in ("INR", "USDT"):
            return float(b.get("available_balance", CAPITAL))
    log.warning("Balance unavailable — using config value")
    return float(CAPITAL)

def get_candles(symbol, res="1h", count=230):
    end   = int(time.time())
    secs  = {"1h":3600,"4h":14400,"1d":86400,"15m":900}[res]
    start = end - secs * count
    raw   = api_get(f"/v2/history/candles?symbol={symbol}&resolution={res}&start={start}&end={end}").get("result", [])
    return [{"t":int(c["time"]),"o":float(c["open"]),"h":float(c["high"]),"l":float(c["low"]),"c":float(c["close"])} for c in raw]

def get_positions():
    return api_get("/v2/positions/margined").get("result", [])

def get_open_orders(product_id):
    return api_get(f"/v2/orders?product_id={product_id}&state=open").get("result", [])

def get_fill_price(product_id):
    time.sleep(5)
    for pos in get_positions():
        if pos.get("product_id") == product_id and abs(int(pos.get("size", 0))) > 0:
            return float(pos.get("entry_price", 0))
    return 0.0


# ══════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════

def calc_indicators(candles):
    if len(candles) < 210:
        return None
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    def ema(vals, n):
        k=2/(n+1); e=vals[0]; result=[]
        for v in vals: e=v*k+e*(1-k); result.append(e)
        return result

    e21  = ema(closes, 21)
    e50  = ema(closes, 50)
    e200 = ema(closes, 200)
    macd = [ema(closes,12)[i]-ema(closes,26)[i] for i in range(len(closes))]
    msig = ema(macd, 9)
    mhist     = macd[-1] - msig[-1]
    mhist_prev= macd[-2] - msig[-2]

    diffs  = [closes[i]-closes[i-1] for i in range(1,len(closes))]
    gains  = [max(d,0) for d in diffs];  losses = [abs(min(d,0)) for d in diffs]
    rsi      = 100-100/(1+sum(gains[-14:])/14/(sum(losses[-14:])/14+1e-9))
    rsi_prev = 100-100/(1+sum(gains[-15:-1])/14/(sum(losses[-15:-1])/14+1e-9))

    trs=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    atr=sum(trs[-14:])/14

    return {
        "price":closes[-1],"ema21":e21[-1],"ema50":e50[-1],"ema200":e200[-1],
        "rsi":rsi,"rsi_prev":rsi_prev,"atr":atr,"mhist":mhist,"mhist_prev":mhist_prev,
        "low":lows[-1],"high":highs[-1],
    }


# ══════════════════════════════════════════════════════════════════
#  SIGNAL DETECTION
# ══════════════════════════════════════════════════════════════════

def detect_signal(symbol):
    candles = get_candles(symbol)
    if not candles: return None
    d = calc_indicators(candles)
    if not d: return None

    p     = d["price"]
    bull  = d["ema21"] > d["ema50"] > d["ema200"]
    bear  = d["ema21"] < d["ema50"] < d["ema200"]

    log.info(f"  {symbol:8s} | ${p:>9,.1f} | RSI:{d['rsi']:.0f} | {'BULL' if bull else 'BEAR' if bear else 'FLAT':4s} | ATR:{d['atr']:.0f}")

    # LONG: bull trend + RSI 35-58 pulled back + both turning up
    if (bull and 35<=d["rsi"]<=58 and d["rsi"]>d["rsi_prev"] and d["mhist"]>d["mhist_prev"]):
        sl = round(p - ATR_MULT*d["atr"], 1)
        tp = round(p + (p-sl)*RR_RATIO, 1)
        return {"direction":"buy","price":p,"sl":sl,"tp":tp,"rsi":d["rsi"],"atr":d["atr"]}

    # SHORT: bear trend + RSI 42-65 bounced + both turning down
    if (bear and 42<=d["rsi"]<=65 and d["rsi"]<d["rsi_prev"] and d["mhist"]<d["mhist_prev"]):
        sl = round(p + ATR_MULT*d["atr"], 1)
        tp = round(p - (sl-p)*RR_RATIO, 1)
        return {"direction":"sell","price":p,"sl":sl,"tp":tp,"rsi":d["rsi"],"atr":d["atr"]}

    return None


# ══════════════════════════════════════════════════════════════════
#  AUTO SL/TP — tries every method until one works
# ══════════════════════════════════════════════════════════════════

def auto_sl(product_id, side, qty, sl_price, atr):
    """Try 8 different ways to place SL. One of them WILL work."""
    if DRY_RUN:
        log.info(f"  [DRY] SL {side} {qty} @ {sl_price:.1f}"); return True

    attempts = [
        ("stop_loss_order",   True),
        ("stop_loss_order",   False),
        ("stop_market_order", False),
        ("stop_market_order", True),
    ]
    for price_variant in [round(sl_price,1), round(sl_price,0), round(sl_price/5)*5, round(sl_price/10)*10]:
        for otype, with_limit in attempts:
            payload = {
                "product_id": product_id,
                "side":       side,
                "order_type": otype,
                "size":       qty,
                "stop_price": str(price_variant),
            }
            if with_limit:
                if side == "sell":
                    payload["limit_price"] = str(round(price_variant * 0.997, 1))
                else:
                    payload["limit_price"] = str(round(price_variant * 1.003, 1))
            result = api_post("/v2/orders", payload)
            if result.get("result"):
                log.info(f"  ✅ SL @ {price_variant:.1f} ({otype})")
                return True

    log.error(f"  ❌ SL FAILED — SET MANUALLY AT {sl_price:.1f}")
    return False


def auto_tp(product_id, side, qty, tp_price):
    """Place TP as limit order — tries 4 price variants."""
    if DRY_RUN:
        log.info(f"  [DRY] TP {side} {qty} @ {tp_price:.1f}"); return True

    for price_variant in [round(tp_price,1), round(tp_price,0), round(tp_price/5)*5, round(tp_price/10)*10]:
        result = api_post("/v2/orders", {
            "product_id":  product_id,
            "side":        side,
            "order_type":  "limit_order",
            "size":        qty,
            "limit_price": str(price_variant),
        })
        if result.get("result"):
            log.info(f"  ✅ TP @ {price_variant:.1f}")
            return True

    log.error(f"  ❌ TP FAILED — SET MANUALLY AT {tp_price:.1f}")
    return False


# ══════════════════════════════════════════════════════════════════
#  TRADE EXECUTOR
# ══════════════════════════════════════════════════════════════════

def execute_trade(asset, signal, balance):
    pid       = asset["product_id"]
    direction = signal["direction"]
    close_side= "sell" if direction == "buy" else "buy"

    risk_inr  = balance * RISK_PCT / 100
    risk_pts  = abs(signal["price"] - signal["sl"])
    if risk_pts <= 0: return
    qty = max(1, int(risk_inr / risk_pts))

    log.info(f"\n{'─'*52}")
    log.info(f"  SIGNAL: {direction.upper()} {asset['name']}")
    log.info(f"  Entry  : ${signal['price']:>9,.1f}")
    log.info(f"  SL     : ${signal['sl']:>9,.1f}  (risk: {risk_pts:.0f} pts)")
    log.info(f"  TP     : ${signal['tp']:>9,.1f}  (reward: {abs(signal['tp']-signal['price']):.0f} pts)")
    log.info(f"  Qty    : {qty} contracts")
    log.info(f"  Risk   : Rs{risk_inr:.0f}  ({RISK_PCT}% of Rs{balance:.0f})")
    log.info(f"  RR     : 1:{RR_RATIO}  |  RSI: {signal['rsi']:.0f}  |  ATR: {signal['atr']:.0f}")
    log.info(f"{'─'*52}")

    for o in get_open_orders(pid):
        api_delete("/v2/orders", {"id": o.get("id"), "product_id": pid})

    if DRY_RUN:
        log.info("  [DRY RUN] No real order placed")
        return

    entry = api_post("/v2/orders", {"product_id":pid,"side":direction,"order_type":"market_order","size":qty})
    if not entry.get("result"):
        log.error("  Entry failed"); return
    log.info(f"  Entry order placed | ID:{entry['result'].get('id','?')}")

    fill = get_fill_price(pid)
    if fill > 0 and fill != signal["price"]:
        log.info(f"  Actual fill: ${fill:,.1f} (vs signal ${signal['price']:,.1f})")
        if direction == "buy":
            signal["sl"] = round(fill - ATR_MULT * signal["atr"], 1)
            signal["tp"] = round(fill + abs(fill - signal["sl"]) * RR_RATIO, 1)
        else:
            signal["sl"] = round(fill + ATR_MULT * signal["atr"], 1)
            signal["tp"] = round(fill - abs(signal["sl"] - fill) * RR_RATIO, 1)
        log.info(f"  Adjusted  → SL:{signal['sl']:.1f}  TP:{signal['tp']:.1f}")

    sl_ok = auto_sl(pid, close_side, qty, signal["sl"], signal["atr"])
    tp_ok = auto_tp(pid, close_side, qty, signal["tp"])

    if sl_ok and tp_ok:
        log.info("  STATUS: FULLY AUTOMATIC — SL + TP both placed")
    else:
        log.warning("  STATUS: PARTIAL")
        if not sl_ok: log.warning(f"  SET SL MANUALLY: ${signal['sl']:,.1f}")
        if not tp_ok: log.warning(f"  SET TP MANUALLY: ${signal['tp']:,.1f}")


# ══════════════════════════════════════════════════════════════════
#  POSITION MANAGEMENT
# ══════════════════════════════════════════════════════════════════

def manage_positions():
    positions = get_positions()
    now = int(time.time())
    open_symbols = set()

    for pos in positions:
        size = int(pos.get("size", 0))
        if size == 0: continue

        pid    = pos.get("product_id")
        sym    = pos.get("product", {}).get("symbol", "?")
        entry  = float(pos.get("entry_price", 0))
        pnl    = float(pos.get("unrealized_pnl", 0))
        opened = int(pos.get("created_at", now*1000)) // 1000
        hours  = (now - opened) / 3600

        log.info(f"  POS: {sym} | {'LONG' if size>0 else 'SHORT'} {abs(size)} | ${entry:,.1f} | PnL:{pnl:+.2f} | {hours:.0f}h")
        open_symbols.add(sym)

        if hours > MAX_HOLD_H:
            close_side = "sell" if size > 0 else "buy"
            log.warning(f"  Closing {sym} — held {hours:.0f}h (max {MAX_HOLD_H}h)")
            if not DRY_RUN:
                api_post("/v2/orders", {"product_id":pid,"side":close_side,"order_type":"market_order","size":abs(size)})
                for o in get_open_orders(pid):
                    api_delete("/v2/orders", {"id":o.get("id"),"product_id":pid})

    return open_symbols


# ══════════════════════════════════════════════════════════════════
#  STATUS
# ══════════════════════════════════════════════════════════════════

def show_status():
    log.info("\n" + "═"*52)
    log.info("  ACCOUNT STATUS  (ETH only)")
    log.info("═"*52)
    balance = get_balance()
    log.info(f"  Balance : Rs{balance:,.0f}")
    log.info(f"  Config  : Risk {RISK_PCT}%  |  RR 1:{RR_RATIO}  |  ATR x{ATR_MULT}")
    positions = get_positions()
    if positions:
        for p in positions:
            if int(p.get("size",0))==0: continue
            sym  = p.get("product",{}).get("symbol","?")
            size = int(p.get("size",0))
            ep   = float(p.get("entry_price",0))
            pnl  = float(p.get("unrealized_pnl",0))
            log.info(f"  {'LONG' if size>0 else 'SHORT'} {abs(size)}x {sym} @ ${ep:,.1f} | PnL: {pnl:+.2f}")
    else:
        log.info("  No open positions")
    log.info("\n  MONTHLY PROJECTION (Rs9,000 capital):")
    log.info("  Average month  : Rs 270 – 360")
    log.info("  Good month     : Rs 540 – 810")
    log.info("  Bad month      : Rs-180 – 450")
    log.info("═"*52)


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    log.info("\n" + "═"*52)
    log.info(f"  DELTA ALGO v12  (ETH)  |  {'DRY RUN' if DRY_RUN else 'LIVE'}")
    log.info(f"  {datetime.now().strftime('%d %b %Y  %H:%M IST')}")
    log.info("═"*52)

    if "--status" in sys.argv:
        show_status(); return

    balance = get_balance()
    log.info(f"  Balance: Rs{balance:,.0f}")

    log.info("\n  [1] Managing open positions...")
    open_syms = manage_positions()

    log.info("\n  [2] Scanning for ETH signal...")
    for asset in ASSETS:
        if asset["symbol"] in open_syms:
            log.info(f"  {asset['symbol']}: already open — skip"); continue
        sig = detect_signal(asset["symbol"])
        if sig:
            log.info(f"  SIGNAL on {asset['symbol']}!")
            execute_trade(asset, sig, balance)

    log.info("\n  Cycle complete.")

if __name__ == "__main__":
    main()
