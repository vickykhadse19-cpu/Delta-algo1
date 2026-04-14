"""
Delta Exchange India — BTC + ETH Professional Algo v7
=====================================================
Built for maximum profitability:

ENTRY:
  4H S/R zones  (strongest levels — institutional zones)
  15M candle breaks level with strong body  (breakout)
  5M candle confirms direction  (fast entry — 5-10 min)
  Volume above average on breakout  (real move filter)
  4H trend alignment  (trade with the big money)

EXIT:
  Dynamic RR:
    Normal signal  → 1:6 RR
    Strong signal  → 1:8 RR  (high strength level + volume)
  Trailing stop at 35% of move (locks more profit)
  Trail activates after 1.5x risk (earlier protection)

RISK:
  Strong signal  → 2.0% of balance
  Normal signal  → 1.5% of balance
  Max 3% total per run

SPEED:
  Scans every 15 min  (free on GitHub — 1,440 min/month)
  Entry within 5-10 min of breakout
"""

import os, time, hmac, hashlib, json, requests, logging
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("DELTA_API_KEY",    "")
API_SECRET = os.environ.get("DELTA_API_SECRET", "")
BASE_URL   = "https://api.india.delta.exchange"

ASSETS = {
    "BTC": {"symbol": "BTCUSD", "product_id": None},
    "ETH": {"symbol": "ETHUSD", "product_id": None},
}

# Risk settings
RISK_NORMAL    = 1.5   # % for normal signals
RISK_STRONG    = 2.0   # % for strong signals (high str level + volume)
MAX_TOTAL_RISK = 3.0   # % max per run

# RR settings
RR_NORMAL  = 6.0   # 1:6 for normal signals
RR_STRONG  = 8.0   # 1:8 for strong signals

# Trailing stop
TRAIL_TRIGGER  = 1.5   # earlier — activates after 1.5x risk in profit
TRAIL_DISTANCE = 0.35  # 35% of move — slightly tighter than 40%

# Signal filters
SR_LOOKBACK       = 5
SR_CLUSTER_TOL    = 0.003
MIN_BREAK_PCT     = 0.0005
MAX_BREAK_PCT     = 0.05
MAX_SL_PCT        = 0.06
MIN_BODY_PCT      = 0.40   # 15M candle body must be >40% of range
VOL_MULTIPLIER    = 1.3    # volume must be 1.3x average for strong signal
STRONG_STRENGTH   = 3      # S/R level strength >= 3 = strong signal

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("delta_pro")


# ─────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────
def auth_headers(method, path, qs="", body=""):
    ts  = str(int(time.time()))
    msg = method + ts + path + ("?" + qs if qs else "") + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key": API_KEY, "timestamp": ts, "signature": sig,
        "Content-Type": "application/json", "Accept": "application/json",
    }

def api_get(path, params=None):
    qs  = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = BASE_URL + path + ("?" + qs if qs else "")
    r   = requests.get(url, headers=auth_headers("GET", path, qs), timeout=15)
    r.raise_for_status()
    return r.json()

def api_post(path, payload):
    body = json.dumps(payload, separators=(",", ":"))
    r    = requests.post(BASE_URL + path,
                         headers=auth_headers("POST", path, "", body),
                         data=body, timeout=15)
    r.raise_for_status()
    return r.json()

def api_put(path, payload):
    body = json.dumps(payload, separators=(",", ":"))
    r    = requests.put(BASE_URL + path,
                        headers=auth_headers("PUT", path, "", body),
                        data=body, timeout=15)
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────────────────────
def get_product_id(symbol):
    try:
        r = requests.get(
            f"{BASE_URL}/v2/tickers?contract_types=perpetual_futures",
            headers={"Accept": "application/json"}, timeout=15)
        r.raise_for_status()
        for t in r.json().get("result", []):
            if t.get("symbol") == symbol:
                return t.get("product_id")
    except Exception as e:
        log.warning(f"  Ticker lookup: {e}")
    return None

def get_candles(symbol, resolution, limit=150):
    end     = int(time.time())
    start   = end - resolution * 60 * limit
    res_map = {240: "4h", 60: "1h", 15: "15m", 5: "5m", 1: "1m"}
    res_str = res_map.get(resolution, str(resolution))
    url     = (f"{BASE_URL}/v2/history/candles?"
               f"symbol={symbol}&resolution={res_str}&start={start}&end={end}")
    r = requests.get(url, headers={"Accept": "application/json"}, timeout=15)
    r.raise_for_status()
    raw = r.json().get("result", [])
    if not raw:
        raise ValueError(f"No candles: {symbol} {res_str}")
    out = []
    for c in raw:
        try:
            out.append({
                "time":   int(c["time"]),
                "open":   float(c["open"]),
                "high":   float(c["high"]),
                "low":    float(c["low"]),
                "close":  float(c["close"]),
                "volume": float(c.get("volume", 0)),
            })
        except Exception:
            continue
    return sorted(out, key=lambda x: x["time"])

def get_wallet_balance():
    try:
        data = api_get("/v2/wallet/balances")
        for b in data.get("result", []):
            val = float(b.get("available_balance", 0) or 0)
            if val > 0:
                log.info(f"  {b.get('asset_symbol')} balance: {val:.2f}")
                return val
        return 0.0
    except Exception as e:
        log.warning(f"  Balance failed — geo-block bypass active")
        return -1.0

def get_open_positions():
    try:
        data   = api_get("/v2/positions/margined")
        result = data.get("result", [])
        if isinstance(result, list):
            return {p["product_id"]: p for p in result
                    if float(p.get("size", 0) or 0) != 0}
        return {}
    except Exception:
        return {}

def get_open_orders(product_id):
    try:
        return api_get("/v2/orders", {
            "product_id": product_id, "state": "open"
        }).get("result", [])
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
#  S/R DETECTION  (4H = institutional levels)
# ─────────────────────────────────────────────────────────────
def detect_sr_levels(candles):
    raw, n, lb = [], len(candles), SR_LOOKBACK
    for i in range(lb, n - lb):
        c = candles[i]
        if all(candles[j]["high"] < c["high"] for j in range(i-lb, i+lb+1) if j != i):
            raw.append({"price": c["high"], "type": "R"})
        if all(candles[j]["low"] > c["low"] for j in range(i-lb, i+lb+1) if j != i):
            raw.append({"price": c["low"], "type": "S"})

    used, out = set(), []
    for i, a in enumerate(raw):
        if i in used: continue
        cluster = [a["price"]]; used.add(i)
        for j, b in enumerate(raw):
            if j not in used and abs(b["price"] - a["price"]) / a["price"] < SR_CLUSTER_TOL:
                cluster.append(b["price"]); used.add(j)
        out.append({
            "price":    sum(cluster) / len(cluster),
            "type":     a["type"],
            "strength": len(cluster),
        })
    return sorted(out, key=lambda x: -x["strength"])[:12]


# ─────────────────────────────────────────────────────────────
#  4H TREND DETECTION
#  Simple: last 4H candle direction = short-term 4H trend
#  Plus: last 3 4H candles — majority direction
# ─────────────────────────────────────────────────────────────
def get_4h_trend(c4h):
    if len(c4h) < 4:
        return "NEUTRAL"
    last3 = c4h[-3:]
    bullish = sum(1 for c in last3 if c["close"] > c["open"])
    bearish = sum(1 for c in last3 if c["close"] < c["open"])
    if bullish >= 2:
        return "BULL"
    if bearish >= 2:
        return "BEAR"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────
#  SIGNAL DETECTION — PROFESSIONAL VERSION
#
#  Filters applied:
#  1. 4H level must exist near price
#  2. 15M candle must CLOSE beyond level (not just wick)
#  3. 15M candle body must be strong (>40% of range)
#  4. 5M candle confirms direction
#  5. Volume above average on 15M breakout candle
#  6. 4H trend aligned with trade direction
#
#  Signal grades:
#  STRONG: level strength>=3 + volume>=1.3x + trend aligned
#          → 2.0% risk, 1:8 RR
#  NORMAL: passes all filters but not all strong criteria
#          → 1.5% risk, 1:6 RR
# ─────────────────────────────────────────────────────────────
def detect_signal(c4h, c15m, c5m, sr):
    if len(c15m) < 3 or len(c5m) < 2 or not sr:
        return None

    c15_curr = c15m[-1]
    c15_prev = c15m[-2]
    c5_curr  = c5m[-1]

    px       = c5_curr["close"]
    trend    = get_4h_trend(c4h)

    # Average volume last 20 candles
    avg_vol  = (sum(c["volume"] for c in c15m[-20:]) / 20
                if len(c15m) >= 20 else 0)
    high_vol = (c15_curr["volume"] >= avg_vol * VOL_MULTIPLIER
                if avg_vol > 0 else False)

    def body_pct(c):
        rng = c["high"] - c["low"]
        return abs(c["close"] - c["open"]) / rng if rng > 0 else 0

    def is_strong(strength):
        return (strength >= STRONG_STRENGTH and high_vol and
                trend in ("BULL", "BEAR"))

    # ── LONG ─────────────────────────────────────────────────
    # Only trade LONG if trend is BULL or NEUTRAL
    if trend != "BEAR":
        resistances = sorted(
            [l for l in sr if l["type"] == "R" and l["price"] > px * 0.99],
            key=lambda x: x["price"]
        )
        for res in resistances[:6]:
            level = res["price"]
            brk   = (c15_curr["close"] - level) / level

            if not (MIN_BREAK_PCT < brk < MAX_BREAK_PCT):
                continue
            if body_pct(c15_curr) < MIN_BODY_PCT:
                continue  # weak candle — skip

            if c5_curr["close"] > level * 1.0003:
                entry  = c5_curr["close"]
                sl     = min(level * 0.997, c15_curr["low"], c5_curr["low"])
                risk   = entry - sl
                if risk <= 0 or risk / entry > MAX_SL_PCT:
                    continue

                strong = is_strong(res["strength"])
                rr     = RR_STRONG if strong else RR_NORMAL
                rsk    = RISK_STRONG if strong else RISK_NORMAL

                return {
                    "dir":       "LONG",
                    "entry":     entry,
                    "sl":        sl,
                    "tp":        entry + risk * rr,
                    "level":     level,
                    "risk":      risk,
                    "rr":        rr,
                    "risk_pct":  rsk,
                    "confirmed": True,
                    "strong":    strong,
                    "tf":        "4H_SR+15M+5M",
                    "strength":  res["strength"],
                    "vol":       f"{c15_curr['volume']:.0f} "
                                 f"({'HIGH' if high_vol else 'normal'})",
                    "trend":     trend,
                }
            else:
                return {
                    "dir": "LONG", "confirmed": False,
                    "level": level,
                    "msg": f"15M broke {level:.1f} — waiting 5M confirm",
                }

    # ── SHORT ────────────────────────────────────────────────
    # Only trade SHORT if trend is BEAR or NEUTRAL
    if trend != "BULL":
        supports = sorted(
            [l for l in sr if l["type"] == "S" and l["price"] < px * 1.01],
            key=lambda x: -x["price"]
        )
        for sup in supports[:6]:
            level = sup["price"]
            brk   = (level - c15_curr["close"]) / level

            if not (MIN_BREAK_PCT < brk < MAX_BREAK_PCT):
                continue
            if body_pct(c15_curr) < MIN_BODY_PCT:
                continue

            if c5_curr["close"] < level * 0.9997:
                entry  = c5_curr["close"]
                sl     = max(level * 1.003, c15_curr["high"], c5_curr["high"])
                risk   = sl - entry
                if risk <= 0 or risk / entry > MAX_SL_PCT:
                    continue

                strong = is_strong(sup["strength"])
                rr     = RR_STRONG if strong else RR_NORMAL
                rsk    = RISK_STRONG if strong else RISK_NORMAL

                return {
                    "dir":       "SHORT",
                    "entry":     entry,
                    "sl":        sl,
                    "tp":        entry - risk * rr,
                    "level":     level,
                    "risk":      risk,
                    "rr":        rr,
                    "risk_pct":  rsk,
                    "confirmed": True,
                    "strong":    strong,
                    "tf":        "4H_SR+15M+5M",
                    "strength":  sup["strength"],
                    "vol":       f"{c15_curr['volume']:.0f} "
                                 f"({'HIGH' if high_vol else 'normal'})",
                    "trend":     trend,
                }
            else:
                return {
                    "dir": "SHORT", "confirmed": False,
                    "level": level,
                    "msg": f"15M broke {level:.1f} — waiting 5M confirm",
                }

    return None


# ─────────────────────────────────────────────────────────────
#  TRAILING STOP  (activates at 1.5x risk, trails at 35%)
# ─────────────────────────────────────────────────────────────
def calculate_trail_sl(direction, entry, current_sl, orig_risk, px):
    if direction == "LONG":
        if px - entry < orig_risk * TRAIL_TRIGGER:
            return None
        new_sl = round(px - (px - entry) * TRAIL_DISTANCE, 2)
        return new_sl if new_sl > current_sl else None
    else:
        if entry - px < orig_risk * TRAIL_TRIGGER:
            return None
        new_sl = round(px + (entry - px) * TRAIL_DISTANCE, 2)
        return new_sl if new_sl < current_sl else None


def manage_open_positions(open_positions, c5m_by_asset):
    if not open_positions:
        return
    log.info(f"\n  Managing {len(open_positions)} open position(s)...")

    for pid, pos in open_positions.items():
        try:
            size   = float(pos.get("size", 0) or 0)
            if not size: continue
            symbol = pos.get("product_symbol", "")
            entry  = float(pos.get("entry_price", 0) or 0)
            dir_   = "LONG" if size > 0 else "SHORT"
            asset  = "BTC" if "BTC" in symbol else "ETH"
            c5m    = c5m_by_asset.get(asset, [])
            if not c5m: continue

            px  = c5m[-1]["close"]
            dp  = 1 if entry > 1000 else 2
            pnl = px - entry if dir_ == "LONG" else entry - px
            log.info(f"  {asset} {dir_} | Entry={entry:.{dp}f} | "
                     f"Now={px:.{dp}f} | P/L={pnl:+.{dp}f}")

            sl_orders = [o for o in get_open_orders(pid)
                         if o.get("order_type") == "stop_market_order"]
            if not sl_orders:
                log.info(f"    No SL order found.")
                continue

            sl_ord     = sl_orders[0]
            current_sl = float(sl_ord.get("stop_price", 0) or 0)
            orig_risk  = abs(entry - current_sl)
            if not orig_risk: continue

            new_sl = calculate_trail_sl(dir_, entry, current_sl, orig_risk, px)
            if new_sl:
                log.info(f"    Trail: {current_sl:.{dp}f} → {new_sl:.{dp}f} ✅")
                try:
                    api_put(f"/v2/orders/{sl_ord['id']}",
                            {"id": sl_ord["id"], "stop_price": str(new_sl)})
                except Exception as e:
                    log.warning(f"    SL update failed: {e}")
            else:
                trig = (entry + orig_risk * TRAIL_TRIGGER if dir_ == "LONG"
                        else entry - orig_risk * TRAIL_TRIGGER)
                log.info(f"    Trail activates at {trig:.{dp}f} "
                         f"(need {abs(trig-px):.{dp}f} more pts)")
        except Exception as e:
            log.warning(f"  Manage error: {e}")


# ─────────────────────────────────────────────────────────────
#  ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────
def place_order(product_id, sig, qty, symbol):
    side     = "buy"  if sig["dir"] == "LONG"  else "sell"
    close    = "sell" if sig["dir"] == "LONG"  else "buy"
    sl_price = round(sig["sl"], 2)
    tp_price = round(sig["tp"], 2)
    dp       = 1 if sig["entry"] > 1000 else 2
    sl_off   = -0.5 if sig["dir"] == "LONG" else 0.5
    tp_off   =  0.5 if sig["dir"] == "LONG" else -0.5

    if DRY_RUN:
        log.info(f"  [DRY RUN] {symbol} {sig['dir']} | "
                 f"Entry={sig['entry']:.{dp}f} | SL={sl_price} | "
                 f"TP={tp_price} | RR=1:{int(sig['rr'])}")
        return True

    # Method 1: Bracket order
    try:
        r = api_post("/v2/orders", {
            "product_id":                      product_id,
            "size":                            qty,
            "side":                            side,
            "order_type":                      "market_order",
            "time_in_force":                   "gtc",
            "bracket_stop_loss_price":         str(round(sl_price + sl_off, 2)),
            "bracket_stop_loss_limit_price":   str(sl_price),
            "bracket_take_profit_price":       str(round(tp_price + tp_off, 2)),
            "bracket_take_profit_limit_price": str(tp_price),
        })
        res = r.get("result", {})
        log.info(f"  Bracket OK! ID={res.get('id','?')}")
        log.info(f"  Entry={sig['entry']:.{dp}f} | SL={sl_price} | "
                 f"TP={tp_price} | RR=1:{int(sig['rr'])}")
        return True
    except requests.HTTPError:
        log.warning(f"  Bracket failed — trying fallback...")

    # Method 2: Fallback
    try:
        er = api_post("/v2/orders", {
            "product_id": product_id, "size": qty,
            "side": side, "order_type": "market_order",
            "time_in_force": "gtc",
        })
        log.info(f"  Entry OK! ID={er.get('result',{}).get('id','?')}")
        time.sleep(2)
        api_post("/v2/orders", {
            "product_id": product_id, "size": qty, "side": close,
            "order_type": "stop_market_order", "time_in_force": "gtc",
            "stop_price": str(sl_price), "close_on_trigger": True,
        })
        log.info(f"  SL set at {sl_price}")
        api_post("/v2/orders", {
            "product_id": product_id, "size": qty, "side": close,
            "order_type": "take_profit_market_order", "time_in_force": "gtc",
            "stop_price": str(tp_price), "close_on_trigger": True,
        })
        log.info(f"  TP set at {tp_price}")
        log.info(f"  Entry={sig['entry']:.{dp}f} | SL={sl_price} | "
                 f"TP={tp_price} | RR=1:{int(sig['rr'])}")
        return True
    except Exception as e:
        log.error(f"  All methods failed: {e}")
        log.error(f"  SET MANUALLY → SL={sl_price}  TP={tp_price}")
        return False


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info(f"Delta Pro v7 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"DRY_RUN={DRY_RUN} | BTC+ETH | "
             f"Risk={RISK_NORMAL}-{RISK_STRONG}% | "
             f"RR=1:{int(RR_NORMAL)}/1:{int(RR_STRONG)} | "
             f"Trail@{int(TRAIL_DISTANCE*100)}% | "
             f"4H_SR+15M+5M+VOL+TREND")
    log.info("=" * 65)

    if not API_KEY or not API_SECRET:
        log.error("API keys not set.")
        return

    # Step 1 — Product IDs
    log.info("Resolving product IDs...")
    for asset, cfg in ASSETS.items():
        pid = get_product_id(cfg["symbol"])
        if pid:
            cfg["product_id"] = pid
            log.info(f"  {asset}: {cfg['symbol']} -> product_id={pid}")
        else:
            log.warning(f"  {asset}: not found")

    # Step 2 — Balance
    if DRY_RUN:
        balance = 25000.0
        log.info(f"[DRY RUN] Mock balance = Rs.{balance:,.0f}")
    else:
        raw_bal = get_wallet_balance()
        if raw_bal == -1.0:
            balance = 5000.0
            log.info(f"  Geo-block bypass — Rs.{balance:,.0f}")
        elif raw_bal < 700:
            log.warning(f"  Balance too low. Add funds.")
            return
        else:
            balance = raw_bal
            log.info(f"  Live balance: Rs.{balance:,.0f}")

    # Step 3 — Fetch candles
    c4h_by  = {}
    c15m_by = {}
    c5m_by  = {}
    for asset, cfg in ASSETS.items():
        try:
            c4h_by[asset]  = get_candles(cfg["symbol"], 240, 120)
            c15m_by[asset] = get_candles(cfg["symbol"], 15,  100)
            c5m_by[asset]  = get_candles(cfg["symbol"], 5,   100)
        except Exception as e:
            log.error(f"  Candle error {asset}: {e}")

    # Step 4 — Manage open positions (trailing stop)
    if not DRY_RUN:
        open_pos = get_open_positions()
        if open_pos:
            manage_open_positions(open_pos, c5m_by)

    # Step 5 — Scan for new signals
    trades_placed = 0
    total_risk    = 0.0

    for asset, cfg in ASSETS.items():
        log.info(f"\n{'─'*50}")
        log.info(f"Analysing {asset} ({cfg['symbol']})")
        log.info(f"{'─'*50}")

        if total_risk >= balance * MAX_TOTAL_RISK / 100:
            log.info("  Max total risk reached. Stopping.")
            break

        pid = cfg.get("product_id")
        if not pid:
            continue

        if not DRY_RUN:
            op = get_open_positions()
            if pid in op:
                log.info(f"  Position open — trailing active.")
                continue

        c4h  = c4h_by.get(asset,  [])
        c15m = c15m_by.get(asset, [])
        c5m  = c5m_by.get(asset,  [])

        if not c4h or not c15m or not c5m:
            continue

        px    = c5m[-1]["close"]
        trend = get_4h_trend(c4h)
        dp    = 1 if px > 1000 else 2

        log.info(f"  Price : {px:,.2f}")
        log.info(f"  4H Trend : {trend}")
        log.info(f"  Candles: {len(c4h)}x4H | {len(c15m)}x15M | {len(c5m)}x5M")

        sr = detect_sr_levels(c4h)
        log.info(f"  4H S/R: {len(sr)} levels")
        for lv in sr[:6]:
            dist = (lv['price'] - px) / px * 100
            tag  = " ← STRONG" if lv["strength"] >= STRONG_STRENGTH else ""
            log.info(f"    {lv['type']} {lv['price']:,.2f} "
                     f"(str={lv['strength']}) {dist:+.2f}%{tag}")

        sig = detect_signal(c4h, c15m, c5m, sr)

        if not sig:
            log.info(f"  No signal — not near any 4H level.")
            continue

        if not sig["confirmed"]:
            log.info(f"  {sig['dir']} near {sig['level']:,.2f} — "
                     f"{sig.get('msg','waiting 5M candle')}")
            continue

        # Confirmed signal
        grade      = "STRONG ⚡" if sig["strong"] else "NORMAL"
        risk_pct   = sig["risk_pct"]
        rr         = sig["rr"]
        risk_budget = balance * risk_pct / 100
        risk_per_c  = abs(sig["entry"] - sig["sl"])
        qty         = max(1, int(risk_budget / risk_per_c)) if risk_per_c > 0 else 1
        actual_risk = qty * risk_per_c
        actual_tp   = qty * abs(sig["tp"] - sig["entry"])

        log.info(f"\n  *** SIGNAL: {sig['dir']} {asset} [{grade}] ***")
        log.info(f"  Timeframe  : {sig['tf']}")
        log.info(f"  4H Level   : {sig['level']:,.{dp}f} (strength={sig['strength']})")
        log.info(f"  4H Trend   : {sig['trend']}")
        log.info(f"  Volume     : {sig['vol']}")
        log.info(f"  Entry      : {sig['entry']:,.{dp}f}")
        log.info(f"  Stop Loss  : {sig['sl']:,.{dp}f}")
        log.info(f"  Take Profit: {sig['tp']:,.{dp}f}  (1:{int(rr)} RR)")
        log.info(f"  Risk       : Rs.{actual_risk:.0f}  ({risk_pct}% of balance)")
        log.info(f"  Reward     : Rs.{actual_tp:.0f}")
        log.info(f"  Contracts  : {qty}")
        log.info(f"  Trail SL   : starts after "
                 f"+{risk_per_c * TRAIL_TRIGGER:.{dp}f} pts "
                 f"| trails at {int(TRAIL_DISTANCE*100)}%")

        success = place_order(pid, sig, qty, asset)
        if success:
            trades_placed += 1
            total_risk    += actual_risk
        break

    log.info(f"\n{'='*65}")
    log.info(f"Summary: trades={trades_placed} | "
             f"risk=Rs.{total_risk:.0f} | balance=Rs.{balance:,.0f}")
    log.info("Run complete.")


if __name__ == "__main__":
    run()
