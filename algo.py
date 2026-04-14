"""
Delta Exchange India â€” BTC + ETH Multi-Timeframe Breakout Algo v3
=================================================================
Strategy : 4H S/R detection -> 1H confirm -> 15M entry
           Bracket order with SL + TP 1:4
           Fallback: separate SL/TP orders if bracket fails
Exchange : Delta Exchange India
Assets   : BTCUSD + ETHUSD
Risk     : 1.5% per trade
Runs     : Every 15 minutes via GitHub Actions
"""

import os, time, hmac, hashlib, json, requests, logging
from datetime import datetime, timezone

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  CONFIGURATION
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
API_KEY    = os.environ.get("DELTA_API_KEY",    "")
API_SECRET = os.environ.get("DELTA_API_SECRET", "")
BASE_URL   = "https://api.india.delta.exchange"

ASSETS = {
    "BTC": {"symbol": "BTCUSD", "product_id": None},
    "ETH": {"symbol": "ETHUSD", "product_id": None},
}

RISK_PERCENT   = 1.5
RR_RATIO       = 4.0
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
log = logging.getLogger("delta_algo_v3")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  AUTH
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  MARKET DATA
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        log.warning(f"  Balance fetch failed ({e}) â€” using fallback")
        return -1.0


def get_open_position(product_id):
    try:
        data = api_get("/v2/positions", {"product_id": product_id})
        pos  = data.get("result", {})
        if isinstance(pos, list):
            pos = pos[0] if pos else {}
        size = float(pos.get("size", 0) or 0)
        return pos if size != 0 else None
    except Exception:
        return None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  S/R DETECTION  (4H candles = stronger zones)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  SIGNAL DETECTION  (4H + 1H + 15M confirmation)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
                            "dir": "LONG", "entry": entry, "sl": sl,
                            "tp": entry + risk * RR_RATIO,
                            "level": res["price"], "risk": risk,
                            "confirmed": True, "tf": "4H+1H+15M",
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
                            "dir": "SHORT", "entry": entry, "sl": sl,
                            "tp": entry - risk * RR_RATIO,
                            "level": sup["price"], "risk": risk,
                            "confirmed": True, "tf": "4H+1H+15M",
                        }
            else:
                return {"dir": "SHORT", "confirmed": False, "level": sup["price"]}

    return None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  ORDER EXECUTION
#  Method 1: Bracket order (entry + SL + TP in one)
#  Method 2: Fallback â€” place entry first, then SL/TP separately
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def place_order_with_sl_tp(product_id, sig, qty, symbol):
    side     = "buy"  if sig["dir"] == "LONG"  else "sell"
    close    = "sell" if sig["dir"] == "LONG"  else "buy"
    sl_price = round(sig["sl"], 2)
    tp_price = round(sig["tp"], 2)
    dp       = 1 if sig["entry"] > 1000 else 2

    if DRY_RUN:
        log.info(f"  [DRY RUN] {symbol} {sig['dir']} order simulated.")
        log.info(f"  Entry={sig['entry']:.{dp}f} SL={sl_price} TP={tp_price}")
        return True

    # â”€â”€ Method 1: Bracket order â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    sl_off = -0.5 if sig["dir"] == "LONG" else 0.5
    tp_off =  0.5 if sig["dir"] == "LONG" else -0.5

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

    try:
        resp   = api_post("/v2/orders", bracket_payload)
        result = resp.get("result", {})
        log.info(f"  âœ… Bracket order placed! ID={result.get('id','?')} state={result.get('state','?')}")
        log.info(f"  Entry={sig['entry']:.{dp}f} | SL={sl_price} | TP={tp_price} | RR=1:{int(RR_RATIO)}")
        return True
    except requests.HTTPError as e:
        err = e.response.text if e.response else str(e)
        log.warning(f"  Bracket order failed: {err}")
        log.info(f"  Trying fallback method...")

    # â”€â”€ Method 2: Fallback â€” market entry + separate SL/TP â”€â”€â”€
    try:
        # Step A: Place market entry order
        entry_payload = {
            "product_id":   product_id,
            "size":         qty,
            "side":         side,
            "order_type":   "market_order",
            "time_in_force": "gtc",
        }
        entry_resp = api_post("/v2/orders", entry_payload)
        entry_result = entry_resp.get("result", {})
        log.info(f"  âœ… Entry order placed! ID={entry_result.get('id','?')}")

        # Wait 2 seconds for order to fill
        time.sleep(2)

        # Step B: Place Stop Loss order
        sl_payload = {
            "product_id":   product_id,
            "size":         qty,
            "side":         close,
            "order_type":   "stop_market_order",
            "time_in_force": "gtc",
            "stop_price":   str(sl_price),
            "close_on_trigger": True,
        }
        sl_resp = api_post("/v2/orders", sl_payload)
        log.info(f"  âœ… Stop Loss set at {sl_price}")

        # Step C: Place Take Profit order
        tp_payload = {
            "product_id":   product_id,
            "size":         qty,
            "side":         close,
            "order_type":   "take_profit_market_order",
            "time_in_force": "gtc",
            "stop_price":   str(tp_price),
            "close_on_trigger": True,
        }
        tp_resp = api_post("/v2/orders", tp_payload)
        log.info(f"  âœ… Take Profit set at {tp_price}")
        log.info(f"  Entry={sig['entry']:.{dp}f} | SL={sl_price} | TP={tp_price} | RR=1:{int(RR_RATIO)}")
        return True

    except requests.HTTPError as e:
        err = e.response.text if e.response else str(e)
        log.error(f"  âŒ Fallback also failed: {err}")
        log.error(f"  âš ï¸  MANUAL ACTION NEEDED: Set SL={sl_price} TP={tp_price} on Delta Exchange app!")
        return False
    except Exception as e:
        log.error(f"  âŒ Unexpected error: {e}")
        return False


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  MAIN
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def run():
    log.info("=" * 65)
    log.info(f"Delta Algo v3 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"DRY_RUN={DRY_RUN} | BTC+ETH | Risk={RISK_PERCENT}% | RR=1:{int(RR_RATIO)} | 4H+1H+15M")
    log.info("=" * 65)

    if not API_KEY or not API_SECRET:
        log.error("API keys not set. Add to GitHub Secrets.")
        return

    # Step 1 â€” Product IDs
    log.info("Resolving product IDs...")
    for asset, cfg in ASSETS.items():
        pid = get_product_id(cfg["symbol"])
        if pid:
            cfg["product_id"] = pid
            log.info(f"  {asset}: {cfg['symbol']} -> product_id={pid}")
        else:
            log.warning(f"  {asset}: not found")

    # Step 2 â€” Balance
    if DRY_RUN:
        balance = 25000.0
        log.info(f"[DRY RUN] Mock balance = â‚¹{balance:,.0f}")
    else:
        raw_bal = get_wallet_balance()
        if raw_bal == -1.0:
            balance = 5000.0
            log.info(f"  Geo-block bypass â€” fallback â‚¹{balance:,.0f}")
        elif raw_bal < 700:
            log.warning(f"  Balance â‚¹{raw_bal:.0f} too low. Add funds.")
            return
        else:
            balance = raw_bal
            log.info(f"  Live balance: â‚¹{balance:,.0f} âœ…")

    # Step 3 â€” Analyse assets
    trades_placed = 0
    total_risk    = 0.0

    for asset, cfg in ASSETS.items():
        log.info(f"\n{'â”€'*50}")
        log.info(f"Analysing {asset} ({cfg['symbol']})")
        log.info(f"{'â”€'*50}")

        if total_risk >= balance * 0.04:
            log.info("  Max risk reached. Stopping.")
            break

        pid = cfg.get("product_id")
        if not pid:
            log.warning(f"  No product_id. Skipping.")
            continue

        if not DRY_RUN:
            pos = get_open_position(pid)
            if pos:
                log.info(f"  Open position exists. Skipping.")
                continue

        # Fetch 3 timeframes
        try:
            c4h  = get_candles(cfg["symbol"], 240, 120)
            c1h  = get_candles(cfg["symbol"], 60,  100)
            c15m = get_candles(cfg["symbol"], 15,  96)
            log.info(f"  Candles: {len(c4h)}x4H | {len(c1h)}x1H | {len(c15m)}x15M")
        except Exception as e:
            log.error(f"  Candle error: {e}")
            continue

        px = c15m[-1]["close"] if c15m else c1h[-1]["close"]
        log.info(f"  Price: {px:,.2f}")

        # S/R from 4H
        sr = detect_sr_levels(c4h)
        log.info(f"  4H S/R: {len(sr)} levels")
        for lv in sr[:5]:
            dist = (lv['price'] - px) / px * 100
            log.info(f"    {lv['type']} {lv['price']:,.2f} (str={lv['strength']}) {dist:+.2f}%")

        # Signal
        sig = detect_signal(c4h, c1h, c15m, sr)

        if not sig:
            log.info(f"  No signal.")
            continue

        if not sig["confirmed"]:
            log.info(f"  {sig['dir']} breakout at {sig['level']:,.2f} â€” waiting 15M confirmation.")
            continue

        # Position sizing
        dp          = 1 if sig["entry"] > 1000 else 2
        risk_budget = balance * RISK_PERCENT / 100
        risk_per_c  = abs(sig["entry"] - sig["sl"])
        qty         = max(1, int(risk_budget / risk_per_c)) if risk_per_c > 0 else 1
        actual_risk = qty * risk_per_c

        log.info(f"\n  *** SIGNAL: {sig['dir']} {asset} [{sig['tf']}] ***")
        log.info(f"  Entry     : {sig['entry']:,.{dp}f}")
        log.info(f"  Stop Loss : {sig['sl']:,.{dp}f}")
        log.info(f"  Take Prof : {sig['tp']:,.{dp}f}")
        log.info(f"  Risk      : â‚¹{actual_risk:.0f} ({actual_risk/balance*100:.2f}%)")
        log.info(f"  Contracts : {qty}")

        success = place_order_with_sl_tp(pid, sig, qty, asset)
        if success:
            trades_placed += 1
            total_risk    += actual_risk

        break

    log.info(f"\n{'='*65}")
    log.info(f"Summary: trades={trades_placed} | risk=â‚¹{total_risk:.0f} | balance=â‚¹{balance:,.0f}")
    log.info("Run complete.")


if __name__ == "__main__":
    run()
