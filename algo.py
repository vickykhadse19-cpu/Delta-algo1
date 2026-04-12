"""
Delta Exchange — BTC/ETH S/R Breakout Algo
==========================================
Strategy : 1H S/R detection → 15M confirmation → Bracket order (SL + TP 1:4)
Run      : python algo.py          (manual)
Schedule : GitHub Actions cron     (automatic, free)
"""

import os, time, hmac, hashlib, json, requests, logging
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION  (set these as GitHub Secrets or .env)
# ─────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("DELTA_API_KEY",    "")
API_SECRET = os.environ.get("DELTA_API_SECRET", "")
BASE_URL   = "https://api.delta.exchange"

ASSETS = {
    "BTC": {"symbol": "BTCUSDT", "product_id": None},   # filled at startup
    "ETH": {"symbol": "ETHUSD",  "product_id": None},
}

RISK_PERCENT   = 1.0    # % of available wallet balance to risk per trade
RR_RATIO       = 4.0    # take profit = risk × this (1:4)
MIN_BREAK_PCT  = 0.001  # 1H close must be >0.1% beyond the S/R level
MAX_BREAK_PCT  = 0.025  # ignore if already moved >2.5% past level (chasing)
SR_LOOKBACK    = 5      # bars each side to confirm a swing pivot
SR_CLUSTER_TOL = 0.003  # 0.3%  — cluster nearby levels
MAX_SL_PCT     = 0.04   # skip trade if SL is >4% away (too wide)
DRY_RUN        = os.environ.get("DRY_RUN", "false").lower() == "true"

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("delta_algo")


# ─────────────────────────────────────────────────────────────
#  DELTA EXCHANGE — AUTHENTICATED REQUEST
# ─────────────────────────────────────────────────────────────
def _sign(method: str, path: str, qs: str = "", body: str = "") -> dict:
    """Build signed headers for Delta Exchange API v2."""
    ts  = str(int(time.time()))
    msg = method + ts + path + (("?" + qs) if qs else "") + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key":      API_KEY,
        "timestamp":    ts,
        "signature":    sig,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }


def api_get(path: str, params: dict = None) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = BASE_URL + path + (("?" + qs) if qs else "")
    headers = _sign("GET", path, qs)
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: dict) -> dict:
    body = json.dumps(payload, separators=(",", ":"))
    headers = _sign("POST", path, "", body)
    r = requests.post(BASE_URL + path, headers=headers, data=body, timeout=15)
    r.raise_for_status()
    return r.json()


def api_delete(path: str, payload: dict = None) -> dict:
    body = json.dumps(payload or {}, separators=(",", ":"))
    headers = _sign("DELETE", path, "", body)
    r = requests.delete(BASE_URL + path, headers=headers, data=body, timeout=15)
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────
#  MARKET DATA  (no auth needed)
# ─────────────────────────────────────────────────────────────
def get_candles(symbol: str, resolution: int, limit: int = 110) -> list:
    """
    resolution : minutes (60 = 1H, 15 = 15M)
    Returns list of dicts sorted oldest → newest.
    """
    end   = int(time.time())
    start = end - resolution * 60 * limit
    url   = f"{BASE_URL}/v2/history/candles?symbol={symbol}&resolution={resolution}&start={start}&end={end}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    raw = r.json().get("result", [])
    if not raw:
        raise ValueError(f"No candle data returned for {symbol} {resolution}m")
    candles = [
        {
            "time":   int(c["time"]),
            "open":   float(c["open"]),
            "high":   float(c["high"]),
            "low":    float(c["low"]),
            "close":  float(c["close"]),
            "volume": float(c.get("volume", 0)),
        }
        for c in raw
        if c.get("time")
    ]
    return sorted(candles, key=lambda x: x["time"])


def get_products() -> list:
    r = requests.get(f"{BASE_URL}/v2/products?contract_types=perpetual_futures", timeout=15)
    r.raise_for_status()
    return r.json().get("result", [])


def get_wallet_balance() -> float:
    """Returns available USDT balance."""
    data = api_get("/v2/wallet/balances")
    for b in data.get("result", []):
        if b.get("asset_symbol") in ("USDT", "USD"):
            return float(b.get("available_balance", 0))
    return 0.0


def get_open_position(product_id: int) -> dict | None:
    """Returns open position dict or None."""
    data = api_get("/v2/positions", {"product_id": product_id})
    pos  = data.get("result", {})
    size = float(pos.get("size", 0))
    return pos if size != 0 else None


# ─────────────────────────────────────────────────────────────
#  SUPPORT / RESISTANCE DETECTION
# ─────────────────────────────────────────────────────────────
def detect_sr_levels(candles: list) -> list:
    """
    Swing-pivot method:
      - Swing HIGH : bar whose high is highest among ±SR_LOOKBACK bars
      - Swing LOW  : bar whose low  is lowest  among ±SR_LOOKBACK bars
    Nearby levels (within SR_CLUSTER_TOL) are merged.
    Returns list of dicts: {price, type ('R'|'S'), strength}
    """
    raw, n = [], len(candles)
    lb = SR_LOOKBACK

    for i in range(lb, n - lb):
        c = candles[i]
        is_hi = all(candles[j]["high"] < c["high"] for j in range(i - lb, i + lb + 1) if j != i)
        is_lo = all(candles[j]["low"]  > c["low"]  for j in range(i - lb, i + lb + 1) if j != i)
        if is_hi:
            raw.append({"price": c["high"], "type": "R"})
        if is_lo:
            raw.append({"price": c["low"],  "type": "S"})

    # Cluster
    used, out = set(), []
    for i, a in enumerate(raw):
        if i in used:
            continue
        cluster = [a["price"]]
        used.add(i)
        for j, b in enumerate(raw):
            if j not in used and abs(b["price"] - a["price"]) / a["price"] < SR_CLUSTER_TOL:
                cluster.append(b["price"])
                used.add(j)
        out.append({
            "price":    sum(cluster) / len(cluster),
            "type":     a["type"],
            "strength": len(cluster),
        })

    # Sort by strength, keep top 10
    return sorted(out, key=lambda x: -x["strength"])[:10]


# ─────────────────────────────────────────────────────────────
#  SIGNAL DETECTION
# ─────────────────────────────────────────────────────────────
def detect_signal(c1h: list, c15m: list, sr: list) -> dict | None:
    """
    LONG  : 1H closed above resistance  +  15M candle confirms above level
    SHORT : 1H closed below support     +  15M candle confirms below level

    Returns signal dict or None.
    """
    if len(c1h) < 12 or not sr:
        return None

    last1h  = c1h[-1]
    last15m = c15m[-1] if c15m else None
    px      = last15m["close"] if last15m else last1h["close"]

    # ── LONG signals ──────────────────────────────────────────
    resistances = sorted(
        [l for l in sr if l["type"] == "R" and l["price"] > px * 0.997],
        key=lambda x: x["price"]
    )
    for res in resistances[:4]:
        brk = (last1h["close"] - res["price"]) / res["price"]
        if MIN_BREAK_PCT < brk < MAX_BREAK_PCT:
            if last15m and last15m["close"] > res["price"] * 1.001:
                entry  = last15m["close"]
                sl     = min(res["price"] * 0.999, last15m["low"])
                risk   = entry - sl
                if 0 < risk / entry < MAX_SL_PCT:
                    return {
                        "dir":       "LONG",
                        "entry":     entry,
                        "sl":        sl,
                        "tp":        entry + risk * RR_RATIO,
                        "level":     res["price"],
                        "risk":      risk,
                        "confirmed": True,
                    }
            else:
                return {"dir": "LONG", "confirmed": False, "level": res["price"]}

    # ── SHORT signals ─────────────────────────────────────────
    supports = sorted(
        [l for l in sr if l["type"] == "S" and l["price"] < px * 1.003],
        key=lambda x: -x["price"]
    )
    for sup in supports[:4]:
        brk = (sup["price"] - last1h["close"]) / sup["price"]
        if MIN_BREAK_PCT < brk < MAX_BREAK_PCT:
            if last15m and last15m["close"] < sup["price"] * 0.999:
                entry  = last15m["close"]
                sl     = max(sup["price"] * 1.001, last15m["high"])
                risk   = sl - entry
                if 0 < risk / entry < MAX_SL_PCT:
                    return {
                        "dir":       "SHORT",
                        "entry":     entry,
                        "sl":        sl,
                        "tp":        entry - risk * RR_RATIO,
                        "level":     sup["price"],
                        "risk":      risk,
                        "confirmed": True,
                    }
            else:
                return {"dir": "SHORT", "confirmed": False, "level": sup["price"]}

    return None


# ─────────────────────────────────────────────────────────────
#  POSITION SIZING
# ─────────────────────────────────────────────────────────────
def calc_contracts(balance: float, risk_pct: float, entry: float, sl: float) -> int:
    """
    Risk amount  = balance × risk_pct / 100
    Contract qty = risk_amount / |entry - sl|   (Delta uses USD-settled perps)
    Minimum 1 contract.
    """
    risk_usd  = balance * risk_pct / 100
    per_cont  = abs(entry - sl)
    if per_cont <= 0:
        return 1
    qty = max(1, int(risk_usd / per_cont))
    log.info(f"  Balance ${balance:.2f} | Risk ${risk_usd:.2f} | Per-contract ${per_cont:.2f} → {qty} contracts")
    return qty


# ─────────────────────────────────────────────────────────────
#  ORDER EXECUTION  (bracket order = entry + SL + TP in one)
# ─────────────────────────────────────────────────────────────
def place_bracket_order(product_id: int, sig: dict, qty: int) -> dict:
    """
    Places a market bracket order on Delta Exchange.
    Bracket orders include stop-loss and take-profit attached to entry.
    """
    side        = "buy"  if sig["dir"] == "LONG"  else "sell"
    sl_price    = round(sig["sl"], 2)
    tp_price    = round(sig["tp"], 2)

    # SL trigger is slightly beyond the SL limit to ensure fill
    sl_offset   = -0.5 if sig["dir"] == "LONG" else 0.5
    tp_offset   =  0.5 if sig["dir"] == "LONG" else -0.5

    payload = {
        "product_id":                      product_id,
        "size":                            qty,
        "side":                            side,
        "order_type":                      "market_order",
        "time_in_force":                   "gtc",
        "bracket_stop_loss_price":         str(sl_price + sl_offset),
        "bracket_stop_loss_limit_price":   str(sl_price),
        "bracket_take_profit_price":       str(tp_price + tp_offset),
        "bracket_take_profit_limit_price": str(tp_price),
    }

    log.info(f"  Payload → {json.dumps(payload, indent=2)}")

    if DRY_RUN:
        log.info("  [DRY RUN] Order NOT sent to exchange.")
        return {"result": {"id": "DRY_RUN", "state": "open"}}

    resp = api_post("/v2/orders", payload)
    return resp


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP — runs once per invocation (cron handles repeating)
# ─────────────────────────────────────────────────────────────
def run():
    log.info("=" * 60)
    log.info(f"Delta Exchange Algo  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"DRY_RUN = {DRY_RUN}")
    log.info("=" * 60)

    if not API_KEY or not API_SECRET:
        log.error("DELTA_API_KEY / DELTA_API_SECRET not set. Exiting.")
        return

    # ── Resolve product IDs ───────────────────────────────────
    log.info("Fetching product list...")
    products = get_products()
    for asset, cfg in ASSETS.items():
        match = next((p for p in products if p.get("symbol") == cfg["symbol"]), None)
        if match:
            cfg["product_id"] = match["id"]
            log.info(f"  {asset}: {cfg['symbol']} → product_id={cfg['product_id']}")
        else:
            log.warning(f"  {asset}: symbol {cfg['symbol']} not found in products")

    # ── Wallet balance ────────────────────────────────────────
    balance = get_wallet_balance()
    log.info(f"Wallet balance: ${balance:.2f} USDT")
    if balance < 10:
        log.warning("Balance too low (<$10). Skipping.")
        return

    # ── Analyse each asset ────────────────────────────────────
    for asset, cfg in ASSETS.items():
        log.info(f"\n{'─'*40}")
        log.info(f"Analysing {asset}  ({cfg['symbol']})")
        log.info(f"{'─'*40}")

        pid = cfg.get("product_id")
        if not pid:
            log.warning(f"  No product_id for {asset}, skipping.")
            continue

        # Skip if already in a position
        pos = get_open_position(pid)
        if pos:
            log.info(f"  Open position exists (size={pos.get('size')}), skipping.")
            continue

        # Fetch candles
        try:
            c1h  = get_candles(cfg["symbol"], 60,  100)
            c15m = get_candles(cfg["symbol"], 15,  96)
            log.info(f"  Candles: {len(c1h)} × 1H  |  {len(c15m)} × 15M")
        except Exception as e:
            log.error(f"  Candle fetch failed: {e}")
            continue

        current_price = c1h[-1]["close"]
        log.info(f"  Current price: {current_price:.2f}")

        # Detect S/R levels
        sr = detect_sr_levels(c1h)
        log.info(f"  S/R levels detected: {len(sr)}")
        for l in sr[:5]:
            log.info(f"    {l['type']}  {l['price']:.2f}  (strength {l['strength']})")

        # Detect signal
        sig = detect_signal(c1h, c15m, sr)

        if sig is None:
            log.info("  No signal — no breakout detected.")
            continue

        if not sig["confirmed"]:
            log.info(f"  {sig['dir']} breakout at {sig['level']:.2f} — WAITING for 15M confirmation.")
            continue

        # Confirmed signal
        dp = 1 if sig["entry"] > 1000 else 2
        log.info(f"\n  *** SIGNAL: {sig['dir']} ***")
        log.info(f"  Entry  : {sig['entry']:.{dp}f}")
        log.info(f"  SL     : {sig['sl']:.{dp}f}  (risk = {sig['risk']:.{dp}f})")
        log.info(f"  TP     : {sig['tp']:.{dp}f}  (reward = {sig['risk']*RR_RATIO:.{dp}f})  → 1:{int(RR_RATIO)} RR")
        log.info(f"  Level  : {sig['level']:.{dp}f}  (broken)")

        # Position sizing
        qty = calc_contracts(balance, RISK_PERCENT, sig["entry"], sig["sl"])
        log.info(f"  Contracts : {qty}")

        # Place order
        log.info("  Placing bracket order...")
        try:
            resp = place_bracket_order(pid, sig, qty)
            result = resp.get("result", {})
            order_id = result.get("id", "?")
            state    = result.get("state", "?")
            log.info(f"  ✅ Order placed! ID={order_id}  state={state}")
        except requests.HTTPError as e:
            log.error(f"  ❌ Order failed: {e.response.text if e.response else e}")
        except Exception as e:
            log.error(f"  ❌ Unexpected error: {e}")

        # Only trade one asset per run to control risk
        break

    log.info("\nRun complete.")


if __name__ == "__main__":
    run()
