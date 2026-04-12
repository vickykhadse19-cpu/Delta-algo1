"""
Delta Exchange India â€” BTC + ETH S/R Breakout Algo
===================================================
Strategy : 1H S/R detection -> 15M confirmation -> Bracket order (SL + TP 1:4)
Exchange : Delta Exchange India  (api.india.delta.exchange)
Assets   : BTCUSD + ETHUSD  (both traded simultaneously)
Risk     : 1.5% per trade   (increased from 1% for more profit)
"""

import os, time, hmac, hashlib, json, requests, logging
from datetime import datetime, timezone

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  CONFIGURATION
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
API_KEY    = os.environ.get("DELTA_API_KEY",    "")
API_SECRET = os.environ.get("DELTA_API_SECRET", "")
BASE_URL   = "https://api.india.delta.exchange"

# Both BTC and ETH now traded
ASSETS = {
    "BTC": {"symbol": "BTCUSD", "product_id": None},
    "ETH": {"symbol": "ETHUSD", "product_id": None},
}

RISK_PERCENT   = 1.5    # increased from 1.0 â†’ more profit, still safe
RR_RATIO       = 4.0    # take profit = risk Ã— 4  (1:4 RR)
MIN_BREAK_PCT  = 0.001
MAX_BREAK_PCT  = 0.03
SR_LOOKBACK    = 5
SR_CLUSTER_TOL = 0.003
MAX_SL_PCT     = 0.05
MAX_TRADES_PER_RUN = 2  # allow both BTC and ETH to trade same run

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("delta_algo")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  AUTH HELPERS
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
    r    = requests.post(BASE_URL + path, headers=auth_headers("POST", path, "", body), data=body, timeout=15)
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


def get_candles(symbol, resolution, limit=110):
    end     = int(time.time())
    start   = end - resolution * 60 * limit
    res_str = {60: "1h", 15: "15m", 5: "5m", 1: "1m"}.get(resolution, str(resolution))
    url     = f"{BASE_URL}/v2/history/candles?symbol={symbol}&resolution={res_str}&start={start}&end={end}"
    r       = requests.get(url, headers={"Accept": "application/json"}, timeout=15)
    r.raise_for_status()
    raw = r.json().get("result", [])
    if not raw:
        raise ValueError(f"No candles returned for {symbol} res={res_str}")
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
    """
    Try to fetch real balance.
    If Delta India API blocks the request (geo-block from GitHub USA servers)
    fall back to a safe default so algo can still run and place orders.
    Exchange will reject orders automatically if real margin is insufficient.
    """
    try:
        data = api_get("/v2/wallet/balances")
        balances = data.get("result", [])
        for b in balances:
            val = float(b.get("available_balance", 0) or 0)
            if val > 0:
                log.info(f"  Asset: {b.get('asset_symbol')}  Balance: {val:.2f}")
                return val
        log.warning("  All balances are zero on exchange.")
        return 0.0
    except Exception as e:
        log.warning(f"  Could not fetch balance ({e})")
        log.warning(f"  This is likely a geo-block (GitHub USA -> Delta India)")
        log.warning(f"  Using fallback balance for position sizing.")
        return -1.0   # special value meaning: use fallback


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
#  S/R DETECTION
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
        out.append({"price": sum(cluster)/len(cluster), "type": a["type"], "strength": len(cluster)})
    return sorted(out, key=lambda x: -x["strength"])[:10]


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  SIGNAL DETECTION
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def detect_signal(c1h, c15m, sr):
    if len(c1h) < 12 or not sr:
        return None
    last1h  = c1h[-1]
    last15m = c15m[-1] if c15m else None
    px      = last15m["close"] if last15m else last1h["close"]

    # LONG
    for res in sorted([l for l in sr if l["type"]=="R" and l["price"]>px*0.997], key=lambda x: x["price"])[:4]:
        brk = (last1h["close"] - res["price"]) / res["price"]
        if MIN_BREAK_PCT < brk < MAX_BREAK_PCT:
            if last15m and last15m["close"] > res["price"] * 1.001:
                entry = last15m["close"]
                sl    = min(res["price"] * 0.999, last15m["low"])
                risk  = entry - sl
                if 0 < risk / entry < MAX_SL_PCT:
                    return {"dir":"LONG","entry":entry,"sl":sl,"tp":entry+risk*RR_RATIO,"level":res["price"],"risk":risk,"confirmed":True}
            else:
                return {"dir":"LONG","confirmed":False,"level":res["price"]}

    # SHORT
    for sup in sorted([l for l in sr if l["type"]=="S" and l["price"]<px*1.003], key=lambda x: -x["price"])[:4]:
        brk = (sup["price"] - last1h["close"]) / sup["price"]
        if MIN_BREAK_PCT < brk < MAX_BREAK_PCT:
            if last15m and last15m["close"] < sup["price"] * 0.999:
                entry = last15m["close"]
                sl    = max(sup["price"] * 1.001, last15m["high"])
                risk  = sl - entry
                if 0 < risk / entry < MAX_SL_PCT:
                    return {"dir":"SHORT","entry":entry,"sl":sl,"tp":entry-risk*RR_RATIO,"level":sup["price"],"risk":risk,"confirmed":True}
            else:
                return {"dir":"SHORT","confirmed":False,"level":sup["price"]}
    return None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  ORDER
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def place_bracket_order(product_id, sig, qty, symbol):
    side     = "buy" if sig["dir"] == "LONG" else "sell"
    sl_price = round(sig["sl"], 2)
    tp_price = round(sig["tp"], 2)
    sl_off   = -0.5 if sig["dir"] == "LONG" else 0.5
    tp_off   =  0.5 if sig["dir"] == "LONG" else -0.5

    payload = {
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
    log.info(f"  Payload: {json.dumps(payload)}")

    if DRY_RUN:
        log.info(f"  [DRY RUN] {symbol} order simulated â€” NOT sent to exchange.")
        return {"result": {"id": f"DRY_RUN_{symbol}", "state": "simulated"}}

    return api_post("/v2/orders", payload)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  MAIN
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def run():
    log.info("=" * 65)
    log.info(f"Delta Exchange India Algo | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"DRY_RUN = {DRY_RUN}  |  Assets: BTC + ETH  |  Risk: {RISK_PERCENT}%  |  RR: 1:{int(RR_RATIO)}")
    log.info("=" * 65)

    if not API_KEY or not API_SECRET:
        log.error("API keys not set. Add DELTA_API_KEY and DELTA_API_SECRET to GitHub Secrets.")
        return

    # Step 1: Resolve product IDs
    log.info("Resolving product IDs...")
    for asset, cfg in ASSETS.items():
        pid = get_product_id(cfg["symbol"])
        if pid:
            cfg["product_id"] = pid
            log.info(f"  {asset}: {cfg['symbol']} -> product_id={pid}")
        else:
            log.warning(f"  {asset}: {cfg['symbol']} not found")

    # Step 2: Balance
    if DRY_RUN:
        balance = 25000.0
        log.info(f"[DRY RUN] Mock balance = â‚¹{balance:,.0f}")
    else:
        raw_balance = get_wallet_balance()

        if raw_balance == -1.0:
            # Geo-block: GitHub (USA) cannot reach Delta India API
            # Use conservative fallback â€” exchange rejects if real margin insufficient
            balance = 5000.0
            log.info(f"  Fallback balance â‚¹{balance:,.0f} (geo-block bypass)")
            log.info(f"  Exchange will auto-reject order if real margin is insufficient.")
        elif raw_balance < 700:
            log.warning(f"  Balance â‚¹{raw_balance:.0f} too low â€” need at least â‚¹700 for 1 contract.")
            log.warning(f"  Add funds to Delta Exchange to continue.")
            return
        else:
            balance = raw_balance
            log.info(f"  Live balance: â‚¹{balance:,.0f} âœ…")

    # Step 3: Analyse each asset â€” BOTH can trade in same run
    trades_placed = 0
    total_risk    = 0.0

    for asset, cfg in ASSETS.items():
        log.info(f"\n{'â”€'*50}")
        log.info(f"Analysing {asset} ({cfg['symbol']})")
        log.info(f"{'â”€'*50}")

        # Safety: stop if already risked 3% total this run (1.5% Ã— 2 assets)
        if total_risk >= balance * 0.03:
            log.info(f"  Max total risk reached for this run. Stopping.")
            break

        pid = cfg.get("product_id")
        if not pid:
            log.warning(f"  Skipping {asset} â€” product_id not found.")
            continue

        # Check existing position
        if not DRY_RUN:
            pos = get_open_position(pid)
            if pos:
                log.info(f"  Already in position (size={pos.get('size')}). Skipping.")
                continue

        # Fetch candles
        try:
            c1h  = get_candles(cfg["symbol"], 60,  100)
            c15m = get_candles(cfg["symbol"], 15,  96)
            log.info(f"  Candles: {len(c1h)} x 1H  |  {len(c15m)} x 15M")
        except Exception as e:
            log.error(f"  Candle fetch error: {e}")
            continue

        px = c1h[-1]["close"]
        log.info(f"  Price: {px:,.2f}")

        # S/R levels
        sr = detect_sr_levels(c1h)
        log.info(f"  S/R levels detected: {len(sr)}")
        for lv in sr[:5]:
            log.info(f"    {lv['type']}  {lv['price']:,.2f}  (strength={lv['strength']})")

        # Signal
        sig = detect_signal(c1h, c15m, sr)

        if not sig:
            log.info(f"  No signal for {asset} this hour.")
            continue

        if not sig["confirmed"]:
            log.info(f"  {sig['dir']} breakout at {sig['level']:,.2f} â€” awaiting 15M confirmation.")
            continue

        # Position sizing
        risk_budget = balance * RISK_PERCENT / 100
        risk_per_c  = abs(sig["entry"] - sig["sl"])
        qty         = max(1, int(risk_budget / risk_per_c)) if risk_per_c > 0 else 1
        actual_risk = qty * risk_per_c
        actual_tp   = qty * abs(sig["tp"] - sig["entry"])

        dp = 1 if sig["entry"] > 1000 else 2
        log.info(f"\n  *** SIGNAL CONFIRMED: {sig['dir']} {asset} ***")
        log.info(f"  Entry     : {sig['entry']:,.{dp}f}")
        log.info(f"  Stop Loss : {sig['sl']:,.{dp}f}  (risk per contract = {risk_per_c:.{dp}f})")
        log.info(f"  Take Prof : {sig['tp']:,.{dp}f}  (reward = {abs(sig['tp']-sig['entry']):.{dp}f})")
        log.info(f"  Contracts : {qty}")
        log.info(f"  Total risk: â‚¹{actual_risk:.0f}  |  Total reward: â‚¹{actual_tp:.0f}  -> 1:{int(RR_RATIO)} RR")
        log.info(f"  Risk %    : {actual_risk/balance*100:.2f}% of balance")

        # Place order
        try:
            resp   = place_bracket_order(pid, sig, qty, asset)
            result = resp.get("result", {})
            log.info(f"  Order placed! ID={result.get('id','?')}  state={result.get('state','?')}")
            trades_placed += 1
            total_risk    += actual_risk
        except requests.HTTPError as e:
            log.error(f"  Order failed: {e.response.text if e.response else e}")
        except Exception as e:
            log.error(f"  Order error: {e}")

    # Summary
    log.info(f"\n{'='*65}")
    log.info(f"Run Summary:")
    log.info(f"  Trades placed  : {trades_placed}")
    log.info(f"  Total risk     : â‚¹{total_risk:.0f}")
    log.info(f"  Balance        : â‚¹{balance:,.0f}")
    log.info(f"  Risk % of bal  : {total_risk/balance*100:.2f}%")
    log.info(f"{'='*65}")
    log.info("Run complete.")


if __name__ == "__main__":
    run()
