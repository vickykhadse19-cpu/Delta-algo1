"""
Delta Exchange India — BTC + ETH Ultimate Algo v9
==================================================
STRATEGY: EMA Trend + S/R Confluence

Why this is better:
  Old strategy waited for price to reach S/R levels
  = sometimes waits days for a trade

  New strategy uses EMA crossover on 15M chart
  = signals happen multiple times per day
  = trades with the current momentum
  = S/R used as confirmation, not as trigger

LOGIC:
  LONG  : EMA9 crosses ABOVE EMA21 on 15M
          + price above EMA50
          + 5M candle confirms bullish
          + NOT at a resistance level
          → enter LONG immediately

  SHORT : EMA9 crosses BELOW EMA21 on 15M
          + price below EMA50
          + 5M candle confirms bearish
          + NOT at a support level
          → enter SHORT immediately

PROFIT:
  RR     : 1:3 (faster, more frequent wins)
  Risk   : 1.5% per trade
  Trail  : 30% of move after 1x risk
  
  More trades × decent RR = more monthly profit
  than fewer trades × high RR

RUNS: Every 15 minutes on GitHub Actions (free)
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

RISK_PERCENT   = 1.5    # % per trade
RR_RATIO       = 3.0    # 1:3 — achievable, frequent
TRAIL_TRIGGER  = 1.0    # trail after 1x risk in profit
TRAIL_DISTANCE = 0.30   # 30% of move
MAX_TOTAL_RISK = 3.0    # max % per run
MAX_SL_PCT     = 0.04   # SL max 4% away
SR_LOOKBACK    = 5
SR_CLUSTER_TOL = 0.003

# EMA periods
EMA_FAST   = 9
EMA_SLOW   = 21
EMA_TREND  = 50

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("delta_v9")


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
        log.warning(f"  Ticker: {e}")
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
    except Exception:
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
#  EMA CALCULATION
# ─────────────────────────────────────────────────────────────
def ema(prices, period):
    """Exponential Moving Average."""
    if len(prices) < period:
        return None
    k   = 2 / (period + 1)
    val = sum(prices[:period]) / period  # SMA for seed
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val

def calculate_emas(candles):
    """Returns dict of current EMA values."""
    closes = [c["close"] for c in candles]
    return {
        "fast":  ema(closes, EMA_FAST),
        "slow":  ema(closes, EMA_SLOW),
        "trend": ema(closes, EMA_TREND),
        # Previous candle EMAs for crossover detection
        "fast_prev":  ema(closes[:-1], EMA_FAST)  if len(closes) > EMA_FAST  else None,
        "slow_prev":  ema(closes[:-1], EMA_SLOW)  if len(closes) > EMA_SLOW  else None,
    }


# ─────────────────────────────────────────────────────────────
#  S/R DETECTION  (used as filter — avoid entering at wall)
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
    return sorted(out, key=lambda x: -x["strength"])[:15]


def is_near_sr_wall(px, sr_levels, direction, tolerance=0.005):
    """
    Returns True if price is within 0.5% of a wall that would block the trade.
    LONG blocked by resistance above.
    SHORT blocked by support below.
    """
    for l in sr_levels:
        dist = abs(l["price"] - px) / px
        if dist > tolerance:
            continue
        if direction == "LONG" and l["type"] == "R" and l["price"] > px:
            return True, l["price"]
        if direction == "SHORT" and l["type"] == "S" and l["price"] < px:
            return True, l["price"]
    return False, None


# ─────────────────────────────────────────────────────────────
#  VOLUME ANALYSIS
# ─────────────────────────────────────────────────────────────
def is_volume_ok(candles, multiplier=1.0):
    """Volume on last candle should be above average."""
    if len(candles) < 20:
        return True
    avg = sum(c["volume"] for c in candles[-20:-1]) / 19
    return candles[-1]["volume"] >= avg * multiplier


# ─────────────────────────────────────────────────────────────
#  SIGNAL DETECTION — EMA CROSSOVER + TREND + S/R FILTER
#
#  LONG conditions:
#   1. EMA9 just crossed ABOVE EMA21 (on 15M)
#   2. Price is ABOVE EMA50 (uptrend)
#   3. 5M candle is bullish (close > open)
#   4. Not within 0.5% of a resistance level
#   5. Volume >= average
#
#  SHORT conditions:
#   1. EMA9 just crossed BELOW EMA21 (on 15M)
#   2. Price is BELOW EMA50 (downtrend)
#   3. 5M candle is bearish (close < open)
#   4. Not within 0.5% of a support level
#   5. Volume >= average
#
#  SL placement:
#   LONG  → SL below EMA21 (natural support)
#   SHORT → SL above EMA21 (natural resistance)
# ─────────────────────────────────────────────────────────────
def detect_signal(c15m, c5m, sr_1h, sr_4h):
    if len(c15m) < EMA_TREND + 5 or len(c5m) < 3:
        return None

    curr_15m = c15m[-1]
    curr_5m  = c5m[-1]
    px       = curr_5m["close"]

    # Calculate EMAs on 15M
    e = calculate_emas(c15m)
    if not all([e["fast"], e["slow"], e["trend"],
                e["fast_prev"], e["slow_prev"]]):
        return None

    # Combine S/R from both timeframes
    all_sr = sr_4h + [l for l in sr_1h
                      if not any(abs(l["price"] - s["price"]) / s["price"] < SR_CLUSTER_TOL
                                 for s in sr_4h)]

    dp = 1 if px > 1000 else 2

    log.info(f"  EMA9={e['fast']:.{dp}f} | EMA21={e['slow']:.{dp}f} | "
             f"EMA50={e['trend']:.{dp}f}")

    # ── LONG ─────────────────────────────────────────────────
    # EMA9 crossed above EMA21
    crossed_up = (e["fast_prev"] <= e["slow_prev"] and
                  e["fast"]      >  e["slow"])

    if crossed_up:
        log.info(f"  EMA9 crossed ABOVE EMA21 ← bullish crossover!")

        # Price must be above EMA50 (uptrend)
        if px < e["trend"] * 0.998:
            log.info(f"  LONG blocked — price below EMA50 ({e['trend']:.{dp}f})")
        else:
            # 5M candle must be bullish
            if curr_5m["close"] <= curr_5m["open"]:
                log.info(f"  LONG blocked — 5M candle bearish")
            else:
                # Volume check
                if not is_volume_ok(c15m, 0.8):
                    log.info(f"  LONG blocked — volume too low")
                else:
                    # Not near resistance
                    blocked, wall = is_near_sr_wall(px, all_sr, "LONG")
                    if blocked:
                        log.info(f"  LONG blocked — resistance at {wall:.{dp}f}")
                    else:
                        # Calculate SL below EMA21
                        sl   = min(e["slow"] * 0.998, curr_15m["low"])
                        risk = px - sl
                        if 0 < risk / px < MAX_SL_PCT:
                            return {
                                "dir":       "LONG",
                                "entry":     px,
                                "sl":        round(sl, 2),
                                "tp":        round(px + risk * RR_RATIO, 2),
                                "risk":      risk,
                                "confirmed": True,
                                "signal":    "EMA9 x EMA21 BULL",
                                "ema_fast":  e["fast"],
                                "ema_slow":  e["slow"],
                                "ema_trend": e["trend"],
                            }

    # ── SHORT ────────────────────────────────────────────────
    # EMA9 crossed below EMA21
    crossed_dn = (e["fast_prev"] >= e["slow_prev"] and
                  e["fast"]      <  e["slow"])

    if crossed_dn:
        log.info(f"  EMA9 crossed BELOW EMA21 ← bearish crossover!")

        # Price must be below EMA50 (downtrend)
        if px > e["trend"] * 1.002:
            log.info(f"  SHORT blocked — price above EMA50 ({e['trend']:.{dp}f})")
        else:
            # 5M candle must be bearish
            if curr_5m["close"] >= curr_5m["open"]:
                log.info(f"  SHORT blocked — 5M candle bullish")
            else:
                # Volume check
                if not is_volume_ok(c15m, 0.8):
                    log.info(f"  SHORT blocked — volume too low")
                else:
                    # Not near support
                    blocked, wall = is_near_sr_wall(px, all_sr, "SHORT")
                    if blocked:
                        log.info(f"  SHORT blocked — support at {wall:.{dp}f}")
                    else:
                        # SL above EMA21
                        sl   = max(e["slow"] * 1.002, curr_15m["high"])
                        risk = sl - px
                        if 0 < risk / px < MAX_SL_PCT:
                            return {
                                "dir":       "SHORT",
                                "entry":     px,
                                "sl":        round(sl, 2),
                                "tp":        round(px - risk * RR_RATIO, 2),
                                "risk":      risk,
                                "confirmed": True,
                                "signal":    "EMA9 x EMA21 BEAR",
                                "ema_fast":  e["fast"],
                                "ema_slow":  e["slow"],
                                "ema_trend": e["trend"],
                            }

    if not crossed_up and not crossed_dn:
        # Check if already in crossover (not just happened)
        if e["fast"] > e["slow"] and px > e["trend"]:
            log.info(f"  EMA9 > EMA21, price > EMA50 — uptrend, waiting fresh cross")
        elif e["fast"] < e["slow"] and px < e["trend"]:
            log.info(f"  EMA9 < EMA21, price < EMA50 — downtrend, waiting fresh cross")
        else:
            log.info(f"  No crossover — EMAs not aligned")

    return None


# ─────────────────────────────────────────────────────────────
#  TRAILING STOP
# ─────────────────────────────────────────────────────────────
def calculate_trail_sl(direction, entry, current_sl, orig_risk, px):
    if direction == "LONG":
        if px - entry < orig_risk * TRAIL_TRIGGER: return None
        new_sl = round(px - (px - entry) * TRAIL_DISTANCE, 2)
        return new_sl if new_sl > current_sl else None
    else:
        if entry - px < orig_risk * TRAIL_TRIGGER: return None
        new_sl = round(px + (entry - px) * TRAIL_DISTANCE, 2)
        return new_sl if new_sl < current_sl else None


def manage_open_positions(open_positions, c5m_by_asset):
    if not open_positions: return
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
                log.info(f"    Trail: {current_sl:.{dp}f} → {new_sl:.{dp}f}")
                try:
                    api_put(f"/v2/orders/{sl_ord['id']}",
                            {"id": sl_ord["id"], "stop_price": str(new_sl)})
                    log.info(f"    SL updated ✅")
                except Exception as e:
                    log.warning(f"    SL update failed: {e}")
            else:
                trig = (entry + orig_risk * TRAIL_TRIGGER if dir_ == "LONG"
                        else entry - orig_risk * TRAIL_TRIGGER)
                log.info(f"    Trail at {trig:.{dp}f} "
                         f"(need {abs(trig-px):.{dp}f} more pts)")
        except Exception as e:
            log.warning(f"  Manage error: {e}")


# ─────────────────────────────────────────────────────────────
#  ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────
def place_order(product_id, sig, qty, symbol):
    side     = "buy"  if sig["dir"] == "LONG"  else "sell"
    close    = "sell" if sig["dir"] == "LONG"  else "buy"
    sl_price = sig["sl"]
    tp_price = sig["tp"]
    dp       = 1 if sig["entry"] > 1000 else 2

    if DRY_RUN:
        log.info(f"  [DRY RUN] {symbol} {sig['dir']}")
        log.info(f"  Entry={sig['entry']:.{dp}f} | SL={sl_price} | "
                 f"TP={tp_price} | RR=1:{int(RR_RATIO)}")
        return True

    try:
        # Entry
        er = api_post("/v2/orders", {
            "product_id":    product_id,
            "size":          qty,
            "side":          side,
            "order_type":    "market_order",
            "time_in_force": "gtc",
        })
        log.info(f"  ✅ Entry! ID={er.get('result',{}).get('id','?')}")
        time.sleep(3)

        # Stop Loss
        api_post("/v2/orders", {
            "product_id":       product_id,
            "size":             qty,
            "side":             close,
            "order_type":       "stop_market_order",
            "time_in_force":    "gtc",
            "stop_price":       str(sl_price),
            "close_on_trigger": True,
        })
        log.info(f"  ✅ SL set at {sl_price}")

        # Take Profit
        api_post("/v2/orders", {
            "product_id":       product_id,
            "size":             qty,
            "side":             close,
            "order_type":       "take_profit_market_order",
            "time_in_force":    "gtc",
            "stop_price":       str(tp_price),
            "close_on_trigger": True,
        })
        log.info(f"  ✅ TP set at {tp_price}")
        log.info(f"  ══════════════════════════════")
        log.info(f"  {sig['dir']} {symbol} COMPLETE")
        log.info(f"  Entry : {sig['entry']:.{dp}f}")
        log.info(f"  SL    : {sl_price}")
        log.info(f"  TP    : {tp_price}")
        log.info(f"  RR    : 1:{int(RR_RATIO)}")
        log.info(f"  ══════════════════════════════")
        return True

    except requests.HTTPError as e:
        err = e.response.text if e.response else str(e)
        log.error(f"  ❌ Failed: {err}")
        log.error(f"  MANUAL: SL={sl_price} TP={tp_price}")
        return False
    except Exception as e:
        log.error(f"  ❌ Error: {e}")
        return False


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info(f"Delta Ultimate v9 | "
             f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"DRY_RUN={DRY_RUN} | BTC+ETH | Risk={RISK_PERCENT}% | "
             f"RR=1:{int(RR_RATIO)} | Trail@{int(TRAIL_DISTANCE*100)}% | "
             f"EMA{EMA_FAST}/EMA{EMA_SLOW}/EMA{EMA_TREND} + S/R filter")
    log.info("=" * 65)

    if not API_KEY or not API_SECRET:
        log.error("API keys not set.")
        return

    # Product IDs
    log.info("Resolving product IDs...")
    for asset, cfg in ASSETS.items():
        pid = get_product_id(cfg["symbol"])
        if pid:
            cfg["product_id"] = pid
            log.info(f"  {asset}: {cfg['symbol']} -> product_id={pid}")
        else:
            log.warning(f"  {asset}: not found")

    # Balance
    if DRY_RUN:
        balance = 25000.0
        log.info(f"[DRY RUN] Mock balance = Rs.{balance:,.0f}")
    else:
        raw_bal = get_wallet_balance()
        if raw_bal == -1.0:
            balance = 5000.0
            log.info(f"  Geo-block bypass — Rs.{balance:,.0f}")
        elif raw_bal < 700:
            log.warning(f"  Balance too low.")
            return
        else:
            balance = raw_bal
            log.info(f"  Live balance: Rs.{balance:,.0f}")

    # Candles
    c4h_by  = {}
    c1h_by  = {}
    c15m_by = {}
    c5m_by  = {}
    for asset, cfg in ASSETS.items():
        try:
            c4h_by[asset]  = get_candles(cfg["symbol"], 240, 120)
            c1h_by[asset]  = get_candles(cfg["symbol"], 60,  100)
            c15m_by[asset] = get_candles(cfg["symbol"], 15,  100)
            c5m_by[asset]  = get_candles(cfg["symbol"], 5,   100)
        except Exception as e:
            log.error(f"  Candle error {asset}: {e}")

    # Manage open positions
    if not DRY_RUN:
        open_pos = get_open_positions()
        if open_pos:
            manage_open_positions(open_pos, c5m_by)

    # Scan
    trades_placed = 0
    total_risk    = 0.0

    for asset, cfg in ASSETS.items():
        log.info(f"\n{'─'*50}")
        log.info(f"Analysing {asset} ({cfg['symbol']})")
        log.info(f"{'─'*50}")

        if total_risk >= balance * MAX_TOTAL_RISK / 100:
            log.info("  Max risk reached.")
            break

        pid = cfg.get("product_id")
        if not pid: continue

        if not DRY_RUN:
            op = get_open_positions()
            if pid in op:
                log.info(f"  Position open — trailing active.")
                continue

        c4h  = c4h_by.get(asset,  [])
        c1h  = c1h_by.get(asset,  [])
        c15m = c15m_by.get(asset, [])
        c5m  = c5m_by.get(asset,  [])

        if not c15m or not c5m: continue

        px = c5m[-1]["close"]
        dp = 1 if px > 1000 else 2

        log.info(f"  Price  : {px:,.2f}")
        log.info(f"  Candles: {len(c4h)}x4H | {len(c1h)}x1H | "
                 f"{len(c15m)}x15M | {len(c5m)}x5M")

        # S/R for filtering
        sr_4h = detect_sr_levels(c4h) if c4h else []
        sr_1h = detect_sr_levels(c1h) if c1h else []

        # Signal
        sig = detect_signal(c15m, c5m, sr_1h, sr_4h)

        if not sig:
            continue

        # Position sizing
        risk_budget = balance * RISK_PERCENT / 100
        risk_per_c  = abs(sig["entry"] - sig["sl"])
        qty         = max(1, int(risk_budget / risk_per_c)) if risk_per_c > 0 else 1
        actual_risk = qty * risk_per_c
        actual_tp   = qty * abs(sig["tp"] - sig["entry"])

        log.info(f"\n  *** SIGNAL: {sig['dir']} {asset} ***")
        log.info(f"  Signal  : {sig['signal']}")
        log.info(f"  EMA9    : {sig['ema_fast']:.{dp}f}")
        log.info(f"  EMA21   : {sig['ema_slow']:.{dp}f}")
        log.info(f"  EMA50   : {sig['ema_trend']:.{dp}f}")
        log.info(f"  Entry   : {sig['entry']:.{dp}f}")
        log.info(f"  SL      : {sig['sl']:.{dp}f}  ← auto set")
        log.info(f"  TP      : {sig['tp']:.{dp}f}  ← auto set (1:{int(RR_RATIO)} RR)")
        log.info(f"  Risk    : Rs.{actual_risk:.0f} ({RISK_PERCENT}%)")
        log.info(f"  Reward  : Rs.{actual_tp:.0f}")
        log.info(f"  Qty     : {qty} contracts")

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
