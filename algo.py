"""
Delta Exchange India — BTC + ETH Multi-Timeframe Breakout Algo v4
=================================================================
Strategy : 4H S/R detection -> 1H confirm -> 15M entry
           RR Ratio : 1:6  (better for 4H moves)
           Trailing Stop : moves SL up as price moves in favor
           Not too tight — trails at 40% of move
Exchange : Delta Exchange India
Assets   : BTCUSD + ETHUSD
Risk     : 1.5% per trade
Runs     : Every 15 minutes via GitHub Actions
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

RISK_PERCENT   = 1.5
RR_RATIO       = 6.0    # upgraded from 4.0 → better for 4H moves
TRAIL_TRIGGER  = 2.0    # start trailing only after 2x risk is in profit
                        # prevents premature trailing on small moves
TRAIL_DISTANCE = 0.4    # trail SL at 40% below highest point
                        # not too tight — gives trade room to breathe
SR_LOOKBACK    = 5
SR_CLUSTER_TOL = 0.003
MIN_BREAK_PCT  = 0.001
MAX_BREAK_PCT  = 0.04
MAX_SL_PCT     = 0.06

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("delta_algo_v4")


# ─────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────
def auth_headers(method, path, query_string="", body=""):
    timestamp = str(int(time.time()))
    message   = method + timestamp + path
    if query_string:
        message += "?" + query_string
    message  += body
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return {
        "api-key":      API_KEY,
        "timestamp":    timestamp,
        "signature":    signature,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }

def api_get(path, params=None):
    qs  = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = BASE_URL + path + ("?" + qs if qs else "")
    r   = requests.get(url, headers=auth_headers("GET", path, qs), timeout=15)
    r.raise_for_status()
    return r.json()

def api_post(path, payload):
    body = json.dumps(payload, separators=(",", ":"))
    r    = requests.post(
        BASE_URL + path,
        headers=auth_headers("POST", path, "", body),
        data=body,
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def api_put(path, payload):
    body = json.dumps(payload, separators=(",", ":"))
    r    = requests.put(
        BASE_URL + path,
        headers=auth_headers("PUT", path, "", body),
        data=body,
        timeout=15
    )
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────────────────────
def get_product_id(symbol):
    try:
        url = f"{BASE_URL}/v2/tickers?contract_types=perpetual_futures"
        r   = requests.get(url, headers={"Accept": "application/json"}, timeout=15)
        r.raise_for_status()
        for t in r.json().get("result", []):
            if t.get("symbol") == symbol:
                return t.get("product_id")
    except Exception as e:
        log.warning(f"  Ticker lookup failed: {e}")
    return None


def get_candles(symbol, resolution, limit=150):
    end     = int(time.time())
    start   = end - resolution * 60 * limit
    res_map = {240: "4h", 60: "1h", 15: "15m", 5: "5m", 1: "1m"}
    res_str = res_map.get(resolution, str(resolution))
    url     = f"{BASE_URL}/v2/history/candles?symbol={symbol}&resolution={res_str}&start={start}&end={end}"
    r       = requests.get(url, headers={"Accept": "application/json"}, timeout=15)
    r.raise_for_status()
    raw = r.json().get("result", [])
    if not raw:
        raise ValueError(f"No candles for {symbol} {res_str}")
    candles = []
    for c in raw:
        try:
            candles.append({
                "time":   int(c["time"]),
                "open":   float(c["open"]),
                "high":   float(c["high"]),
                "low":    float(c["low"]),
                "close":  float(c["close"]),
                "volume": float(c.get("volume", 0)),
            })
        except Exception:
            continue
    return sorted(candles, key=lambda x: x["time"])


def get_wallet_balance():
    try:
        data = api_get("/v2/wallet/balances")
        for b in data.get("result", []):
            val = float(b.get("available_balance", 0) or 0)
            if val > 0:
                log.info(f"  Asset: {b.get('asset_symbol')}  Balance: {val:.2f}")
                return val
        return 0.0
    except Exception as e:
        log.warning(f"  Balance fetch failed ({e}) — using fallback")
        return -1.0


def get_open_positions():
    """Returns all open positions as dict keyed by product_id."""
    try:
        data = api_get("/v2/positions/margined")
        result = data.get("result", [])
        if isinstance(result, list):
            return {
                p["product_id"]: p
                for p in result
                if float(p.get("size", 0) or 0) != 0
            }
        return {}
    except Exception:
        return {}


def get_open_orders(product_id):
    """Get open stop/tp orders for a product."""
    try:
        data = api_get("/v2/orders", {
            "product_id": product_id,
            "state": "open",
        })
        return data.get("result", [])
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
#  S/R DETECTION  (4H candles = strongest zones)
# ─────────────────────────────────────────────────────────────
def detect_sr_levels(candles):
    raw, n, lb = [], len(candles), SR_LOOKBACK
    for i in range(lb, n - lb):
        c     = candles[i]
        is_hi = all(candles[j]["high"] < c["high"] for j in range(i-lb, i+lb+1) if j != i)
        is_lo = all(candles[j]["low"]  > c["low"]  for j in range(i-lb, i+lb+1) if j != i)
        if is_hi: raw.append({"price": c["high"], "type": "R"})
        if is_lo: raw.append({"price": c["low"],  "type": "S"})

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
#  SIGNAL DETECTION  (4H + 1H + 15M)
# ─────────────────────────────────────────────────────────────
def detect_signal(c4h, c1h, c15m, sr):
    if len(c4h) < 10 or len(c1h) < 10 or not sr:
        return None

    last4h  = c4h[-1]
    last1h  = c1h[-1]
    last15m = c15m[-1] if c15m else None
    px      = last15m["close"] if last15m else last1h["close"]

    # LONG
    resistances = sorted(
        [l for l in sr if l["type"] == "R" and l["price"] > px * 0.995],
        key=lambda x: x["price"]
    )
    for res in resistances[:5]:
        brk_1h = (last1h["close"] - res["price"]) / res["price"]
        if MIN_BREAK_PCT < brk_1h < MAX_BREAK_PCT:
            if last15m and last15m["close"] > res["price"] * 1.0005:
                if last4h["close"] >= last4h["open"] * 0.999:
                    entry = last15m["close"]
                    sl    = min(res["price"] * 0.998, last15m["low"], last1h["low"])
                    risk  = entry - sl
                    if 0 < risk / entry < MAX_SL_PCT:
                        return {
                            "dir":       "LONG",
                            "entry":     entry,
                            "sl":        sl,
                            "tp":        entry + risk * RR_RATIO,
                            "level":     res["price"],
                            "risk":      risk,
                            "confirmed": True,
                            "tf":        "4H+1H+15M",
                        }
            else:
                return {"dir": "LONG", "confirmed": False, "level": res["price"]}

    # SHORT
    supports = sorted(
        [l for l in sr if l["type"] == "S" and l["price"] < px * 1.005],
        key=lambda x: -x["price"]
    )
    for sup in supports[:5]:
        brk_1h = (sup["price"] - last1h["close"]) / sup["price"]
        if MIN_BREAK_PCT < brk_1h < MAX_BREAK_PCT:
            if last15m and last15m["close"] < sup["price"] * 0.9995:
                if last4h["close"] <= last4h["open"] * 1.001:
                    entry = last15m["close"]
                    sl    = max(sup["price"] * 1.002, last15m["high"], last1h["high"])
                    risk  = sl - entry
                    if 0 < risk / entry < MAX_SL_PCT:
                        return {
                            "dir":       "SHORT",
                            "entry":     entry,
                            "sl":        sl,
                            "tp":        entry - risk * RR_RATIO,
                            "level":     sup["price"],
                            "risk":      risk,
                            "confirmed": True,
                            "tf":        "4H+1H+15M",
                        }
            else:
                return {"dir": "SHORT", "confirmed": False, "level": sup["price"]}

    return None


# ─────────────────────────────────────────────────────────────
#  TRAILING STOP LOGIC
#  Only activates after price moves 2x risk in your favor
#  Trails at 40% of total move — not too tight
#
#  Example (LONG BTC):
#   Entry = 71,628   Risk = 228
#   Trail starts when price hits 72,084 (entry + 2×228)
#   If price reaches 74,000:
#     Total move = 74,000 - 71,628 = 2,372
#     Trail SL   = 74,000 - (2,372 × 0.40) = 73,051
#   Price must DROP 40% of the move to trigger SL
#   Locks in 60% of profits automatically
# ─────────────────────────────────────────────────────────────
def calculate_trail_sl(sig, current_price):
    """
    Returns new SL price if trailing stop should be updated.
    Returns None if not yet triggered or no update needed.
    """
    entry = sig["entry"]
    risk  = sig["risk"]
    orig_sl = sig["sl"]

    if sig["dir"] == "LONG":
        move = current_price - entry
        # Only trail after 2x risk in profit
        if move < risk * TRAIL_TRIGGER:
            return None
        # New SL = current price minus 40% of total move
        new_sl = current_price - (move * TRAIL_DISTANCE)
        # Round to 2 decimal places
        new_sl = round(new_sl, 2)
        # Only update if new SL is higher than original SL
        if new_sl > orig_sl:
            return new_sl
        return None

    else:  # SHORT
        move = entry - current_price
        if move < risk * TRAIL_TRIGGER:
            return None
        new_sl = current_price + (move * TRAIL_DISTANCE)
        new_sl = round(new_sl, 2)
        if new_sl < orig_sl:
            return new_sl
        return None


def update_stop_loss(product_id, order_id, new_sl_price, direction):
    """Update existing stop loss order to new price."""
    try:
        payload = {
            "id":         order_id,
            "stop_price": str(new_sl_price),
        }
        api_put(f"/v2/orders/{order_id}", payload)
        log.info(f"  Trailing SL updated to {new_sl_price}")
        return True
    except Exception as e:
        log.warning(f"  Could not update SL: {e}")
        return False


# ─────────────────────────────────────────────────────────────
#  ORDER EXECUTION
#  Method 1: Bracket order (entry + SL + TP together)
#  Method 2: Fallback — separate entry, SL, TP orders
# ─────────────────────────────────────────────────────────────
def place_order_with_sl_tp(product_id, sig, qty, symbol):
    side     = "buy"  if sig["dir"] == "LONG"  else "sell"
    close    = "sell" if sig["dir"] == "LONG"  else "buy"
    sl_price = round(sig["sl"], 2)
    tp_price = round(sig["tp"], 2)
    dp       = 1 if sig["entry"] > 1000 else 2

    if DRY_RUN:
        log.info(f"  [DRY RUN] {symbol} {sig['dir']} simulated.")
        log.info(f"  Entry={sig['entry']:.{dp}f} | SL={sl_price} | TP={tp_price}")
        log.info(f"  Trail starts after {sig['risk']*TRAIL_TRIGGER:.{dp}f} points profit")
        return True

    sl_off = -0.5 if sig["dir"] == "LONG" else 0.5
    tp_off =  0.5 if sig["dir"] == "LONG" else -0.5

    # Method 1 — Bracket order
    try:
        bracket_payload = {
            "product_id":                      product_id,
            "size":                            qty,
            "side":                            side,
            "order_type":                      "market_order",
            "time_in_force":                   "gtc",
            "bracket_stop_loss_price":         str(round(sl_price + sl_off, 2)),
            "bracket_stop_loss_limit_price":   str(sl_price),
            "bracket_take_profit_price":       str(round(tp_price + tp_off, 2)),
            "bracket_take_profit_limit_price": str(tp_price),
        }
        resp   = api_post("/v2/orders", bracket_payload)
        result = resp.get("result", {})
        log.info(f"  Bracket order placed! ID={result.get('id','?')}")
        log.info(f"  Entry={sig['entry']:.{dp}f} | SL={sl_price} | TP={tp_price} | RR=1:{int(RR_RATIO)}")
        log.info(f"  Trail activates after +{sig['risk']*TRAIL_TRIGGER:.{dp}f} points")
        return True
    except requests.HTTPError as e:
        log.warning(f"  Bracket failed — trying fallback...")

    # Method 2 — Fallback: separate orders
    try:
        entry_resp = api_post("/v2/orders", {
            "product_id":    product_id,
            "size":          qty,
            "side":          side,
            "order_type":    "market_order",
            "time_in_force": "gtc",
        })
        log.info(f"  Entry placed! ID={entry_resp.get('result',{}).get('id','?')}")
        time.sleep(2)

        api_post("/v2/orders", {
            "product_id":       product_id,
            "size":             qty,
            "side":             close,
            "order_type":       "stop_market_order",
            "time_in_force":    "gtc",
            "stop_price":       str(sl_price),
            "close_on_trigger": True,
        })
        log.info(f"  SL set at {sl_price}")

        api_post("/v2/orders", {
            "product_id":       product_id,
            "size":             qty,
            "side":             close,
            "order_type":       "take_profit_market_order",
            "time_in_force":    "gtc",
            "stop_price":       str(tp_price),
            "close_on_trigger": True,
        })
        log.info(f"  TP set at {tp_price}")
        log.info(f"  Entry={sig['entry']:.{dp}f} | SL={sl_price} | TP={tp_price} | RR=1:{int(RR_RATIO)}")
        log.info(f"  Trail activates after +{sig['risk']*TRAIL_TRIGGER:.{dp}f} points")
        return True

    except Exception as e:
        log.error(f"  All methods failed: {e}")
        log.error(f"  SET MANUALLY: SL={sl_price} TP={tp_price}")
        return False


# ─────────────────────────────────────────────────────────────
#  MANAGE OPEN POSITIONS (trailing stop)
# ─────────────────────────────────────────────────────────────
def manage_open_positions(open_positions, c15m_by_asset):
    """
    Check all open positions and update trailing stops if needed.
    Runs every 15 minutes automatically.
    """
    if not open_positions:
        return

    log.info(f"\n  Managing {len(open_positions)} open position(s)...")

    for pid, pos in open_positions.items():
        try:
            size = float(pos.get("size", 0) or 0)
            if size == 0:
                continue

            symbol    = pos.get("product_symbol", "")
            entry     = float(pos.get("entry_price", 0) or 0)
            direction = "LONG" if size > 0 else "SHORT"

            # Get current price from 15M candles
            asset  = "BTC" if "BTC" in symbol else "ETH"
            c15m   = c15m_by_asset.get(asset, [])
            if not c15m:
                continue
            current_price = c15m[-1]["close"]

            # Calculate current profit
            if direction == "LONG":
                profit_pts = current_price - entry
            else:
                profit_pts = entry - current_price

            dp = 1 if entry > 1000 else 2
            log.info(f"  {asset} {direction} | Entry={entry:.{dp}f} | "
                     f"Now={current_price:.{dp}f} | P/L={profit_pts:+.{dp}f}")

            # Reconstruct sig dict for trailing calculation
            # Get original SL from open orders
            orders    = get_open_orders(pid)
            sl_orders = [o for o in orders if o.get("order_type") == "stop_market_order"]

            if not sl_orders:
                log.info(f"    No SL order found — skipping trail")
                continue

            sl_order    = sl_orders[0]
            current_sl  = float(sl_order.get("stop_price", 0) or 0)
            orig_risk   = abs(entry - current_sl)

            if orig_risk <= 0:
                continue

            sig_mock = {
                "dir":    direction,
                "entry":  entry,
                "sl":     current_sl,
                "risk":   orig_risk,
            }

            new_sl = calculate_trail_sl(sig_mock, current_price)

            if new_sl and new_sl != current_sl:
                log.info(f"    Trailing SL: {current_sl:.{dp}f} → {new_sl:.{dp}f}")
                update_stop_loss(pid, sl_order["id"], new_sl, direction)
            else:
                trail_trigger_price = (
                    entry + orig_risk * TRAIL_TRIGGER
                    if direction == "LONG"
                    else entry - orig_risk * TRAIL_TRIGGER
                )
                remaining = abs(trail_trigger_price - current_price)
                log.info(f"    Trail not yet active. Need {remaining:.{dp}f} more points.")

        except Exception as e:
            log.warning(f"  Position management error: {e}")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info(f"Delta Algo v4 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"DRY_RUN={DRY_RUN} | BTC+ETH | Risk={RISK_PERCENT}% | "
             f"RR=1:{int(RR_RATIO)} | Trail@{int(TRAIL_DISTANCE*100)}% | 4H+1H+15M")
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
            log.info(f"  Geo-block bypass — fallback Rs.{balance:,.0f}")
        elif raw_bal < 700:
            log.warning(f"  Balance too low. Add funds.")
            return
        else:
            balance = raw_bal
            log.info(f"  Live balance: Rs.{balance:,.0f}")

    # Step 3 — Fetch candles for all assets first
    c15m_by_asset = {}
    c4h_by_asset  = {}
    c1h_by_asset  = {}

    for asset, cfg in ASSETS.items():
        try:
            c4h  = get_candles(cfg["symbol"], 240, 120)
            c1h  = get_candles(cfg["symbol"], 60,  100)
            c15m = get_candles(cfg["symbol"], 15,  96)
            c4h_by_asset[asset]  = c4h
            c1h_by_asset[asset]  = c1h
            c15m_by_asset[asset] = c15m
        except Exception as e:
            log.error(f"  Candle fetch failed for {asset}: {e}")

    # Step 4 — Manage open positions (trailing stop)
    if not DRY_RUN:
        open_positions = get_open_positions()
        if open_positions:
            manage_open_positions(open_positions, c15m_by_asset)

    # Step 5 — Look for new signals
    trades_placed = 0
    total_risk    = 0.0

    for asset, cfg in ASSETS.items():
        log.info(f"\n{'─'*50}")
        log.info(f"Analysing {asset} ({cfg['symbol']})")
        log.info(f"{'─'*50}")

        if total_risk >= balance * 0.04:
            log.info("  Max risk reached. Stopping.")
            break

        pid = cfg.get("product_id")
        if not pid:
            continue

        # Skip if already in position
        if not DRY_RUN:
            open_positions = get_open_positions()
            if pid in open_positions:
                log.info(f"  Open position exists — trailing stop active.")
                continue

        c4h  = c4h_by_asset.get(asset,  [])
        c1h  = c1h_by_asset.get(asset,  [])
        c15m = c15m_by_asset.get(asset, [])

        if not c4h or not c1h:
            continue

        px = c15m[-1]["close"] if c15m else c1h[-1]["close"]
        log.info(f"  Price: {px:,.2f}")
        log.info(f"  Candles: {len(c4h)}x4H | {len(c1h)}x1H | {len(c15m)}x15M")

        # S/R from 4H
        sr = detect_sr_levels(c4h)
        log.info(f"  4H S/R: {len(sr)} levels")
        for lv in sr[:5]:
            dist = (lv['price'] - px) / px * 100
            log.info(f"    {lv['type']} {lv['price']:,.2f} "
                     f"(str={lv['strength']}) {dist:+.2f}%")

        # Signal
        sig = detect_signal(c4h, c1h, c15m, sr)

        if not sig:
            log.info(f"  No signal.")
            continue

        if not sig["confirmed"]:
            log.info(f"  {sig['dir']} at {sig['level']:,.2f} — waiting 15M.")
            continue

        # Position sizing
        dp          = 1 if sig["entry"] > 1000 else 2
        risk_budget = balance * RISK_PERCENT / 100
        risk_per_c  = abs(sig["entry"] - sig["sl"])
        qty         = max(1, int(risk_budget / risk_per_c)) if risk_per_c > 0 else 1
        actual_risk = qty * risk_per_c
        actual_tp   = qty * abs(sig["tp"] - sig["entry"])

        log.info(f"\n  *** SIGNAL: {sig['dir']} {asset} [{sig['tf']}] ***")
        log.info(f"  Entry      : {sig['entry']:,.{dp}f}")
        log.info(f"  Stop Loss  : {sig['sl']:,.{dp}f}")
        log.info(f"  Take Profit: {sig['tp']:,.{dp}f}  (1:{int(RR_RATIO)} RR)")
        log.info(f"  Trail SL   : activates after +"
                 f"{sig['risk']*TRAIL_TRIGGER:.{dp}f} points profit")
        log.info(f"  Trail dist : {int(TRAIL_DISTANCE*100)}% of move"
                 f" (not too tight)")
        log.info(f"  Risk       : Rs.{actual_risk:.0f} "
                 f"({actual_risk/balance*100:.2f}%)")
        log.info(f"  Reward     : Rs.{actual_tp:.0f}")
        log.info(f"  Contracts  : {qty}")

        success = place_order_with_sl_tp(pid, sig, qty, asset)
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
