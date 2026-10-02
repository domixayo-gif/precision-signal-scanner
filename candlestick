"""
candlestick_patterns.py
=======================
Candlestick confirmation library for ZETA V1 (scanner.py).

Rules
-----
* A candlestick pattern NEVER triggers a trade by itself.
* It only counts inside the correct MARKET CONTEXT:
    5M structure -> 1M alignment -> 1M pullback -> support/resistance
    -> pattern -> room for the 2-minute expiry -> fresh closed candle.
* One setup = one decision (evaluate_confirmation_context returns ONE dict).
* Closed candles only. Nothing here reads a candle that has not closed.
* New patterns are added with @register_pattern and nothing else changes.

Candle format (same as scanner.py normalize_candles):
    {"from": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}
"""

# ============================================================
# CONFIG
# ============================================================

DEFAULT_CONFIG = {
    "atr_period": 14,

    # pattern geometry
    "min_range_atr": 0.5,
    "prior_move_bars": 4,
    "prior_move_atr": 0.4,
    "min_pattern_quality": 0.45,

    # confirmed support / resistance zones (2+ touches)
    "zone_pivot_window": 3,
    "zone_cluster_atr": 0.5,
    "zone_min_touches": 2,
    "zone_touch_atr": 0.15,
    "zone_pierce_atr": 0.6,
    "zone_lookback": 150,

    # fallback: recent, prominent swing high / low (single touch)
    "swing_pivot_window": 4,
    "swing_lookback_1m": 60,
    "swing_lookback_5m": 30,
    "swing_pivot_window_5m": 3,
    "swing_min_prominence_atr": 1.0,

    # 1M pullback / retest (moderate)
    "pullback_lookback": 8,
    "pullback_min_atr": 0.45,
    "pullback_max_atr": 4.0,
    "pullback_min_bars_since_extreme": 2,

    # room for the 2-minute expiry (moderate)
    "room_min_atr": 0.70,

    # freshness: the bot scans many assets one after another
    "max_entry_delay_sec": 45,

    # 5M structure + 1M alignment
    "structure_fast": 9,
    "structure_slow": 21,
    "structure_pivot_window": 2,
    "align_tolerance_atr": 0.15,
    "align_slow_slope_floor_atr": 0.02,
}

# Every key the scanner may read from a decision. All of them are ALWAYS present.
DECISION_KEYS = (
    "approved", "direction", "patterns", "confirmation_text", "quality",
    "reasons", "approval_reason", "structure_5m", "alignment_1m",
    "zone", "zone_source", "pullback", "room", "timing", "all_detected",
)


def _cfg(override=None):
    cfg = dict(DEFAULT_CONFIG)
    if override:
        cfg.update(override)
    return cfg


# ============================================================
# SMALL HELPERS
# ============================================================

def _clamp01(x):
    return max(0.0, min(1.0, x))


def _metrics(c):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    rng = max(h - l, 1e-12)
    body = abs(cl - o)
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    return o, h, l, cl, rng, body, upper, lower


def average_true_range(candles, period=14):
    if len(candles) < 2:
        return 0.0
    trs = []
    for k in range(max(1, len(candles) - period), len(candles)):
        c = candles[k]
        p = candles[k - 1]["close"]
        trs.append(max(c["high"] - c["low"],
                       abs(c["high"] - p),
                       abs(c["low"] - p)))
    return sum(trs) / len(trs) if trs else 0.0


def _ema(values, period):
    if len(values) < period:
        return []
    mult = 2.0 / (period + 1.0)
    out = [None] * len(values)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for k in range(period, len(values)):
        prev = (values[k] - prev) * mult + prev
        out[k] = prev
    return out


def _prior_move(candles, i, bars):
    """Net close-to-close move INTO candle i (candle i itself excluded)."""
    if i < 2:
        return 0.0
    start = max(0, i - 1 - bars)
    return candles[i - 1]["close"] - candles[start]["close"]


# ============================================================
# PATTERN REGISTRY  (add new patterns here, nothing else changes)
# ============================================================

PATTERN_REGISTRY = []


def register_pattern(name, candles_needed=1):
    """
    Decorator. A pattern function has the signature

        fn(candles, i, atr, cfg) -> None | (bias, quality)

    bias    : "bullish" or "bearish"
    quality : 0.0 - 1.0
    i       : index of the LAST candle of the pattern
    """
    def decorator(fn):
        for entry in PATTERN_REGISTRY:
            if entry["name"] == name:
                entry["fn"] = fn
                entry["needed"] = candles_needed
                return fn
        PATTERN_REGISTRY.append(
            {"name": name, "fn": fn, "needed": candles_needed}
        )
        return fn
    return decorator


# ============================================================
# WICK PATTERNS (shared engine)
# ============================================================

def _wick_pattern(candles, i, atr, cfg, *, bullish, wick,
                  long_min_range, other_max_range, body_max_range=None):
    o, h, l, c, rng, body, up, lo = _metrics(candles[i])

    if rng < cfg["min_range_atr"] * atr:
        return None

    long_wick, short_wick = (lo, up) if wick == "lower" else (up, lo)

    if long_wick / rng < long_min_range:
        return None
    if long_wick < 2.0 * max(body, rng * 0.02):
        return None
    if short_wick / rng > other_max_range:
        return None
    if body_max_range is not None and body / rng > body_max_range:
        return None

    prior = _prior_move(candles, i, cfg["prior_move_bars"])
    need = cfg["prior_move_atr"] * atr
    if bullish and prior > -need:
        return None
    if not bullish and prior < need:
        return None

    quality = (
        0.45 * _clamp01((long_wick / rng - 0.45) / 0.35)
        + 0.25 * _clamp01(1.0 - short_wick / (other_max_range * rng + 1e-12))
        + 0.30 * _clamp01(abs(prior) / (1.5 * atr))
    )
    return ("bullish" if bullish else "bearish", round(quality, 3))


@register_pattern("Hammer")
def _hammer(candles, i, atr, cfg):
    return _wick_pattern(candles, i, atr, cfg, bullish=True, wick="lower",
                         long_min_range=0.55, other_max_range=0.25)


@register_pattern("Inverted Hammer")
def _inverted_hammer(candles, i, atr, cfg):
    res = _wick_pattern(candles, i, atr, cfg, bullish=True, wick="upper",
                        long_min_range=0.55, other_max_range=0.20)
    if res:
        return (res[0], round(res[1] * 0.85, 3))
    return None


@register_pattern("Shooting Star")
def _shooting_star(candles, i, atr, cfg):
    return _wick_pattern(candles, i, atr, cfg, bullish=False, wick="upper",
                         long_min_range=0.55, other_max_range=0.25)


@register_pattern("Bullish Pin Bar")
def _bullish_pin(candles, i, atr, cfg):
    return _wick_pattern(candles, i, atr, cfg, bullish=True, wick="lower",
                         long_min_range=0.66, other_max_range=0.15,
                         body_max_range=0.30)


@register_pattern("Bearish Pin Bar")
def _bearish_pin(candles, i, atr, cfg):
    return _wick_pattern(candles, i, atr, cfg, bullish=False, wick="upper",
                         long_min_range=0.66, other_max_range=0.15,
                         body_max_range=0.30)


# ============================================================
# ENGULFING
# ============================================================

def _engulfing(candles, i, atr, cfg, bullish):
    if i < 1:
        return None
    prev, cur = candles[i - 1], candles[i]
    p_body = abs(prev["close"] - prev["open"])
    c_body = abs(cur["close"] - cur["open"])
    c_rng = cur["high"] - cur["low"]

    if p_body <= 0 or c_rng < cfg["min_range_atr"] * atr:
        return None

    if bullish:
        ok = (prev["close"] < prev["open"] and cur["close"] > cur["open"]
              and cur["open"] <= prev["close"] and cur["close"] >= prev["open"])
    else:
        ok = (prev["close"] > prev["open"] and cur["close"] < cur["open"]
              and cur["open"] >= prev["close"] and cur["close"] <= prev["open"])
    if not ok or c_body < p_body:
        return None

    prior = _prior_move(candles, i, cfg["prior_move_bars"])
    need = cfg["prior_move_atr"] * atr
    if bullish and prior > -need:
        return None
    if not bullish and prior < need:
        return None

    quality = (
        0.50 * _clamp01((c_body / p_body - 1.0) / 1.0)
        + 0.20 * _clamp01(c_body / c_rng)
        + 0.30 * _clamp01(abs(prior) / (1.5 * atr))
    )
    return ("bullish" if bullish else "bearish", round(max(quality, 0.35), 3))


@register_pattern("Bullish Engulfing", 2)
def _bull_engulf(candles, i, atr, cfg):
    return _engulfing(candles, i, atr, cfg, True)


@register_pattern("Bearish Engulfing", 2)
def _bear_engulf(candles, i, atr, cfg):
    return _engulfing(candles, i, atr, cfg, False)


# ============================================================
# MORNING / EVENING STAR
# ============================================================

def _star(candles, i, atr, cfg, bullish):
    if i < 2:
        return None
    c1, c2, c3 = candles[i - 2], candles[i - 1], candles[i]
    b1 = abs(c1["close"] - c1["open"])
    b2 = abs(c2["close"] - c2["open"])
    b3 = abs(c3["close"] - c3["open"])
    r1 = c1["high"] - c1["low"]
    r3 = c3["high"] - c3["low"]

    if b1 <= 0 or r1 < 0.8 * atr or b1 < 0.5 * r1:
        return None
    if b2 > 0.4 * b1 or b3 < 0.5 * max(r3, 1e-12):
        return None

    mid1 = (c1["open"] + c1["close"]) / 2.0

    if bullish:
        ok = (c1["close"] < c1["open"]
              and max(c2["open"], c2["close"]) <= mid1
              and c3["close"] > c3["open"] and c3["close"] > mid1)
    else:
        ok = (c1["close"] > c1["open"]
              and min(c2["open"], c2["close"]) >= mid1
              and c3["close"] < c3["open"] and c3["close"] < mid1)
    if not ok:
        return None

    quality = (
        0.40 * _clamp01(1.0 - b2 / (0.4 * b1))
        + 0.35 * _clamp01(abs(c3["close"] - mid1) / (0.5 * b1))
        + 0.25 * _clamp01(b1 / (1.2 * atr))
    )
    return ("bullish" if bullish else "bearish", round(max(quality, 0.45), 3))


@register_pattern("Morning Star", 3)
def _morning_star(candles, i, atr, cfg):
    return _star(candles, i, atr, cfg, True)


@register_pattern("Evening Star", 3)
def _evening_star(candles, i, atr, cfg):
    return _star(candles, i, atr, cfg, False)


# ============================================================
# DOJI / REJECTION CANDLE (bias comes from the dominant wick)
# ============================================================

@register_pattern("Doji / Rejection Candle")
def _doji_rejection(candles, i, atr, cfg):
    o, h, l, c, rng, body, up, lo = _metrics(candles[i])

    if rng < cfg["min_range_atr"] * atr or body / rng > 0.12:
        return None

    prior = _prior_move(candles, i, cfg["prior_move_bars"])
    need = cfg["prior_move_atr"] * atr

    if lo / rng >= 0.45 and lo >= 1.5 * up and prior <= -need:
        bias, wick = "bullish", lo
    elif up / rng >= 0.45 and up >= 1.5 * lo and prior >= need:
        bias, wick = "bearish", up
    else:
        return None     # balanced doji = indecision, not a confirmation

    quality = (
        0.55 * _clamp01((wick / rng - 0.40) / 0.35)
        + 0.45 * _clamp01(abs(prior) / (1.5 * atr))
    )
    return (bias, round(quality * 0.8, 3))


# ------------------------------------------------------------
# Template for a new pattern:
#
# @register_pattern("Tweezer Bottom", 2)
# def _tweezer_bottom(candles, i, atr, cfg):
#     ...
#     return ("bullish", 0.6)     # or None
# ------------------------------------------------------------


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def _pivots(candles, window):
    highs, lows = [], []
    for k in range(window, len(candles) - window):
        seg = candles[k - window:k + window + 1]
        if candles[k]["high"] >= max(x["high"] for x in seg):
            highs.append((k, candles[k]["high"]))
        if candles[k]["low"] <= min(x["low"] for x in seg):
            lows.append((k, candles[k]["low"]))
    return highs, lows


def find_sr_zones(candles, atr, cfg=None):
    """
    PREFERRED levels: swing highs + lows clustered into zones, kept only when
    touched at least zone_min_touches times. Direction-neutral (a broken
    support that becomes resistance works naturally).
    """
    cfg = _cfg(cfg)
    if atr <= 0 or len(candles) < cfg["zone_pivot_window"] * 2 + 3:
        return []

    highs, lows = _pivots(candles, cfg["zone_pivot_window"])
    levels = sorted([p for _, p in highs] + [p for _, p in lows])
    if not levels:
        return []

    groups, group = [], [levels[0]]
    for price in levels[1:]:
        if price - group[0] <= cfg["zone_cluster_atr"] * atr:
            group.append(price)
        else:
            groups.append(group)
            group = [price]
    groups.append(group)

    zones = []
    for g in groups:
        if len(g) >= cfg["zone_min_touches"]:
            zones.append({
                "low": min(g),
                "high": max(g),
                "level": sum(g) / len(g),
                "touches": len(g),
                "source": "zone",
                "kind": None,
            })
    return zones


def find_swing_levels(candles, atr, window, lookback, cfg=None):
    """
    FALLBACK levels: a recent, PROMINENT swing low / high.
    A swing counts only if price moved away from it by at least
    swing_min_prominence_atr on BOTH sides, so random wiggles are ignored.
    Pivots need `window` closed candles after them, so nothing here can
    use future data.
    """
    cfg = _cfg(cfg)
    n = len(candles)
    if atr <= 0 or n < window * 2 + 3:
        return []

    min_prom = cfg["swing_min_prominence_atr"] * atr
    first = max(window, n - lookback)
    swings = []

    for k in range(first, n - window):
        left = candles[k - window:k]
        right = candles[k + 1:k + 1 + window]
        seg = candles[k - window:k + window + 1]

        if candles[k]["low"] <= min(x["low"] for x in seg):
            prom = min(max(x["high"] for x in left),
                       max(x["high"] for x in right)) - candles[k]["low"]
            if prom >= min_prom:
                p = candles[k]["low"]
                swings.append({
                    "low": p - 0.05 * atr, "high": p + 0.25 * atr,
                    "level": p, "touches": 1,
                    "source": "swing", "kind": "support",
                })

        if candles[k]["high"] >= max(x["high"] for x in seg):
            prom = candles[k]["high"] - max(min(x["low"] for x in left),
                                            min(x["low"] for x in right))
            if prom >= min_prom:
                p = candles[k]["high"]
                swings.append({
                    "low": p - 0.25 * atr, "high": p + 0.05 * atr,
                    "level": p, "touches": 1,
                    "source": "swing", "kind": "resistance",
                })
    return swings


def _touches_support(low, close, zones, atr, cfg):
    tol = cfg["zone_touch_atr"] * atr
    pierce = cfg["zone_pierce_atr"] * atr
    for z in zones:                      # confirmed zones come first in the list
        if z.get("kind") == "resistance":
            continue
        if z["low"] - pierce <= low <= z["high"] + tol and close >= z["low"]:
            return z
    return None


def _touches_resistance(high, close, zones, atr, cfg):
    tol = cfg["zone_touch_atr"] * atr
    pierce = cfg["zone_pierce_atr"] * atr
    for z in zones:
        if z.get("kind") == "support":
            continue
        if z["low"] - tol <= high <= z["high"] + pierce and close <= z["high"]:
            return z
    return None


# ============================================================
# PATTERN DETECTION (separate, modular entry point)
# ============================================================

def detect_candlestick_patterns(candles, index=None, atr=None,
                                zones=None, cfg=None):
    """
    Scan one candle position against the whole registry.

    Returns a list of dicts (best quality first):
        name, bias, quality, candle_index, candle_time,
        at_support, at_resistance, zone
    Several patterns can be returned for the same candle.
    `zones` should list confirmed zones BEFORE fallback swings.
    """
    cfg = _cfg(cfg)
    if not candles:
        return []

    i = len(candles) - 1 if index is None else index
    if atr is None or atr <= 0:
        atr = average_true_range(candles[:i + 1], cfg["atr_period"])
    if atr <= 0:
        return []

    found = []
    for entry in PATTERN_REGISTRY:
        start = i - (entry["needed"] - 1)
        if start < 0:
            continue
        try:
            res = entry["fn"](candles, i, atr, cfg)
        except Exception:
            continue
        if not res:
            continue

        bias, quality = res
        span = candles[start:i + 1]
        lowest = min(x["low"] for x in span)
        highest = max(x["high"] for x in span)
        close = candles[i]["close"]

        sup = _touches_support(lowest, close, zones, atr, cfg) if zones else None
        res_zone = _touches_resistance(highest, close, zones, atr, cfg) if zones else None

        found.append({
            "name": entry["name"],
            "bias": bias,
            "quality": quality,
            "candle_index": i,
            "candle_time": candles[i]["from"],
            "at_support": sup is not None,
            "at_resistance": res_zone is not None,
            "zone": sup if bias == "bullish" else res_zone,
        })

    found.sort(key=lambda d: d["quality"], reverse=True)
    return found


# ============================================================
# 5M STRUCTURE  +  1M ALIGNMENT
# ============================================================

def market_structure(candles_5m, cfg=None):
    """
    bullish : fast EMA > slow EMA, price above slow EMA, no LH+LL breakdown
    bearish : mirror
    neutral : anything else
    """
    cfg = _cfg(cfg)
    result = {"trend": "neutral", "reason": "insufficient data"}
    if len(candles_5m) < cfg["structure_slow"] + 10:
        return result

    closes = [c["close"] for c in candles_5m]
    fast = _ema(closes, cfg["structure_fast"])
    slow = _ema(closes, cfg["structure_slow"])
    if not fast or not slow or fast[-1] is None or slow[-1] is None:
        return result
    f, s, price = fast[-1], slow[-1], closes[-1]

    highs, lows = _pivots(candles_5m[-80:], cfg["structure_pivot_window"])
    hh = len(highs) >= 2 and highs[-1][1] > highs[-2][1]
    lh = len(highs) >= 2 and highs[-1][1] < highs[-2][1]
    hl = len(lows) >= 2 and lows[-1][1] > lows[-2][1]
    ll = len(lows) >= 2 and lows[-1][1] < lows[-2][1]

    if f > s and price > s and not (lh and ll):
        trend = "bullish"
    elif f < s and price < s and not (hh and hl):
        trend = "bearish"
    else:
        trend = "neutral"

    return {"trend": trend, "ema_fast": f, "ema_slow": s,
            "higher_high": hh, "higher_low": hl,
            "lower_high": lh, "lower_low": ll, "reason": trend}


def one_minute_alignment(candles_1m, bullish, atr, cfg=None):
    """
    Moderate 1M EMA 9/21 direction check (not a second trigger):
      * EMA 9 on the trend side of EMA 21
      * close not more than a small tolerance beyond EMA 21
        (a pullback may dip to EMA 21, it may not break it)
      * EMA 21 not turning against the trend over the last 3 candles
    """
    cfg = _cfg(cfg)
    closes = [c["close"] for c in candles_1m]
    fast = _ema(closes, cfg["structure_fast"])
    slow = _ema(closes, cfg["structure_slow"])

    if (not fast or not slow or len(slow) < 5
            or fast[-1] is None or slow[-1] is None or slow[-4] is None
            or atr <= 0):
        return {"ok": False, "reason": "not enough data for 1M EMA alignment"}

    f, s, price = fast[-1], slow[-1], closes[-1]
    tol = cfg["align_tolerance_atr"] * atr
    floor = cfg["align_slow_slope_floor_atr"] * atr
    slope = s - slow[-4]

    if bullish:
        if not f > s:
            return {"ok": False, "reason": "1M EMA 9 is not above EMA 21"}
        if price < s - tol:
            return {"ok": False, "reason": "1M close broke below EMA 21"}
        if slope < -floor:
            return {"ok": False, "reason": "1M EMA 21 is falling"}
    else:
        if not f < s:
            return {"ok": False, "reason": "1M EMA 9 is not below EMA 21"}
        if price > s + tol:
            return {"ok": False, "reason": "1M close broke above EMA 21"}
        if slope > floor:
            return {"ok": False, "reason": "1M EMA 21 is rising"}

    return {"ok": True, "reason": "aligned"}


# ============================================================
# PULLBACK / ROOM / TIMING
# ============================================================

def pullback_check(candles_1m, atr, bullish, cfg=None):
    """
    A GENUINE, MODERATE retracement into the confirmation candle.

      * depth from the recent extreme is at least pullback_min_atr
      * but not deeper than pullback_max_atr (that is a reversal)
      * the extreme is at least 2 candles old (real move away from it)
      * the candle before the confirmation candle closed back below
        (bullish) / above (bearish) the extreme candle's close
    Only closed candles BEFORE the confirmation candle are used for the
    extreme; the confirmation candle contributes its own low/high (the retest).
    """
    cfg = _cfg(cfg)
    i = len(candles_1m) - 1
    window = candles_1m[max(0, i - cfg["pullback_lookback"]):i]

    if len(window) < 4 or atr <= 0:
        return {"ok": False, "depth_atr": 0.0, "bars_since_extreme": 0,
                "reason": "not enough candles to measure a pullback"}

    last = candles_1m[i]

    if bullish:
        ext = max(range(len(window)), key=lambda k: window[k]["high"])
        depth = window[ext]["high"] - last["low"]
        retraced = window[-1]["close"] < window[ext]["close"]
    else:
        ext = min(range(len(window)), key=lambda k: window[k]["low"])
        depth = last["high"] - window[ext]["low"]
        retraced = window[-1]["close"] > window[ext]["close"]

    bars_since = len(window) - 1 - ext
    depth_atr = round(depth / atr, 2)

    if depth_atr < cfg["pullback_min_atr"]:
        reason = (f"no genuine pullback (depth {depth_atr} ATR, "
                  f"need {cfg['pullback_min_atr']})")
        ok = False
    elif depth_atr > cfg["pullback_max_atr"]:
        reason = (f"pullback too deep ({depth_atr} ATR, "
                  f"max {cfg['pullback_max_atr']}): possible reversal")
        ok = False
    elif bars_since < cfg["pullback_min_bars_since_extreme"] or not retraced:
        reason = "no real retracement: price is still at its extreme"
        ok = False
    else:
        reason = "ok"
        ok = True

    return {"ok": ok, "depth_atr": depth_atr,
            "bars_since_extreme": bars_since, "reason": reason}


def room_check(price, zones, atr, bullish, cfg=None):
    """
    Room before the next opposing CONFIRMED zone (2+ touches).
    Fallback swings are not used here: the swing the pullback started from
    is the natural target and must not block the trade.
    """
    cfg = _cfg(cfg)
    if atr <= 0:
        return {"ok": False, "room_atr": 0.0}

    confirmed = [z for z in zones if z.get("source") == "zone"]

    if bullish:
        gaps = [z["low"] - price for z in confirmed if z["low"] > price]
    else:
        gaps = [price - z["high"] for z in confirmed if z["high"] < price]

    if not gaps:
        return {"ok": True, "room_atr": None}

    room_atr = round(min(gaps) / atr, 2)
    return {"ok": room_atr >= cfg["room_min_atr"], "room_atr": room_atr}


def entry_timing_ok(candle, now_ts, timeframe=60, cfg=None):
    cfg = _cfg(cfg)
    delay = now_ts - (candle["from"] + timeframe)
    return {"ok": 0 <= delay <= cfg["max_entry_delay_sec"],
            "delay_sec": int(delay)}


# ============================================================
# ONE SETUP = ONE DECISION
# ============================================================

def evaluate_confirmation_context(direction, candles_1m, candles_5m,
                                  atr_1m, now_ts, cfg=None,
                                  timeframe=60, timeframe_5m=300):
    """
    direction : "CALL" or "PUT" (set by the ZETA V1 trend state in scanner.py)

    Every check below is mandatory. A pattern alone can never approve a
    trade. Returns ONE decision dict containing every key in DECISION_KEYS.
    """
    cfg = _cfg(cfg)
    bullish = direction == "CALL"
    want = "bullish" if bullish else "bearish"
    reasons = []

    decision = {
        "approved": False,
        "direction": direction,
        "patterns": [],
        "confirmation_text": "",
        "quality": 0.0,
        "reasons": reasons,
        "approval_reason": "",
        "structure_5m": None,
        "alignment_1m": None,
        "zone": None,
        "zone_source": None,
        "pullback": None,
        "room": None,
        "timing": None,
        "all_detected": [],
    }

    # ---- closed candles only (no lookahead) -------------------
    if not candles_1m or now_ts < candles_1m[-1]["from"] + timeframe:
        reasons.append("confirmation candle is not closed yet")
        return decision

    if candles_5m and now_ts < candles_5m[-1]["from"] + timeframe_5m:
        candles_5m = candles_5m[:-1]          # drop the still-forming 5M candle

    if len(candles_1m) < 40 or len(candles_5m) < 30:
        reasons.append("not enough candle data")
        return decision

    if atr_1m is None or atr_1m <= 0:
        reasons.append("1M ATR is not available")
        return decision

    i = len(candles_1m) - 1
    last = candles_1m[i]

    # ---- 1. 5M structure --------------------------------------
    structure = market_structure(candles_5m, cfg)
    decision["structure_5m"] = structure["trend"]
    if structure["trend"] != want:
        reasons.append(
            f"5M structure is {structure['trend']}, {direction} needs {want}"
        )

    # ---- 2. 1M EMA 9/21 alignment -----------------------------
    align = one_minute_alignment(candles_1m, bullish, atr_1m, cfg)
    decision["alignment_1m"] = align["ok"]
    if not align["ok"]:
        reasons.append(align["reason"])

    # ---- 3. genuine, moderate 1M pullback ---------------------
    pb = pullback_check(candles_1m, atr_1m, bullish, cfg)
    decision["pullback"] = pb
    if not pb["ok"]:
        reasons.append(pb["reason"])

    # ---- 4. support / resistance (confirmed first, swing fallback) --
    atr_5m = average_true_range(candles_5m, cfg["atr_period"])

    confirmed = find_sr_zones(candles_5m, atr_5m, cfg)
    confirmed += find_sr_zones(candles_1m[-cfg["zone_lookback"]:], atr_1m, cfg)

    swings = find_swing_levels(
        candles_1m, atr_1m, cfg["swing_pivot_window"],
        cfg["swing_lookback_1m"], cfg)
    swings += find_swing_levels(
        candles_5m, atr_5m, cfg["swing_pivot_window_5m"],
        cfg["swing_lookback_5m"], cfg)

    zones = confirmed + swings          # confirmed zones are matched first

    # ---- 5. candlestick confirmation --------------------------
    detected = detect_candlestick_patterns(candles_1m, i, atr_1m, zones, cfg)
    decision["all_detected"] = [
        {"name": p["name"], "bias": p["bias"], "quality": p["quality"],
         "at_support": p["at_support"], "at_resistance": p["at_resistance"]}
        for p in detected
    ]

    usable = [p for p in detected
              if p["bias"] == want and p["quality"] >= cfg["min_pattern_quality"]]

    if not detected:
        reasons.append("no candlestick pattern on the closed candle")
    elif not usable:
        names = ", ".join(f"{p['name']} ({p['bias']}, q={p['quality']})"
                          for p in detected)
        reasons.append(f"patterns found but none usable for {direction}: {names}")

    if usable:
        key = "at_support" if bullish else "at_resistance"
        at_level = [p for p in usable if p[key]]
        if not at_level:
            reasons.append(
                "pattern is not at a confirmed zone or a meaningful recent swing "
                + ("support" if bullish else "resistance")
            )
        # prefer a confirmed zone over a swing fallback, then quality
        at_level.sort(key=lambda p: (p["zone"]["source"] == "zone", p["quality"]),
                      reverse=True)
        usable = at_level
        if usable:
            decision["zone"] = usable[0]["zone"]
            decision["zone_source"] = (
                "confirmed 2+ touch zone" if usable[0]["zone"]["source"] == "zone"
                else "recent swing fallback"
            )

    # ---- 6. room for the 2-minute expiry ----------------------
    room = room_check(last["close"], confirmed, atr_1m, bullish, cfg)
    decision["room"] = room
    if not room["ok"]:
        reasons.append(
            f"not enough room before the opposing zone "
            f"({room['room_atr']} ATR, need {cfg['room_min_atr']})"
        )

    # ---- 7. fresh closed candle -------------------------------
    timing = entry_timing_ok(last, now_ts, timeframe, cfg)
    decision["timing"] = timing
    if not timing["ok"]:
        reasons.append(
            f"entry not fresh ({timing['delay_sec']}s after candle close, "
            f"max {cfg['max_entry_delay_sec']}s)"
        )

    # ---- final: ONE decision ----------------------------------
    if not reasons and usable:
        decision["approved"] = True
        decision["patterns"] = [p["name"] for p in usable]
        decision["confirmation_text"] = " + ".join(decision["patterns"])
        decision["quality"] = round(max(p["quality"] for p in usable), 3)

        z = decision["zone"]
        room_text = ("no opposing confirmed zone ahead"
                     if room["room_atr"] is None
                     else f"{room['room_atr']} ATR of room")
        decision["approval_reason"] = (
            f"5M {structure['trend']} | 1M EMA 9/21 aligned | "
            f"1M pullback {pb['depth_atr']} ATR over "
            f"{pb['bars_since_extreme']} candles | "
            f"{'support' if bullish else 'resistance'} = "
            f"{decision['zone_source']} {z['low']:.5f}-{z['high']:.5f} "
            f"({z['touches']} touch{'es' if z['touches'] != 1 else ''}) | "
            f"confirmation {decision['confirmation_text']} "
            f"(q={decision['quality']}) | {room_text} | "
            f"entry {timing['delay_sec']}s after close"
        )

    return decision
