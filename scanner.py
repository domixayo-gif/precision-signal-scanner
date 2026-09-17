#!/usr/bin/env python3
"""
PRECISION SCANNER V5.1
Twelve Data single-request-per-market scanner.

Research/test framework only.
No trade execution. No profit guarantee.

Main design:
- Twelve Data only
- ONE API request per market
- 1-minute candles downloaded once
- 5-minute candles built locally
- 6 markets scanned per cycle
- 20 FX markets rotated automatically
- 5-minute reference expiry
- Telegram alerts
- GitHub tracker
- /win SIGNAL_ID
- /loss SIGNAL_ID
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


# ============================================================
# CONFIGURATION
# ============================================================

APP_NAME = "PRECISION SCANNER V5.1"
VERSION = "V5.1"

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

TRACKER_FILE = Path("tracker.json")

# Current Twelve Data account reports 8 credits/minute.
# Keep this below the limit.
MARKETS_PER_CYCLE = 6

# Scanner timing.
SCAN_INTERVAL = 60

# Number of 1-minute candles downloaded for each market.
# 400 x 1-minute candles gives ~80 five-minute candles.
OUTPUT_SIZE = 400

# Data freshness.
MAX_FX_DATA_AGE_MINUTES = 15

# Strategy thresholds.
MIN_SCORE = 85
MIN_DOMINANCE = 3
MIN_ADX = 18.0
MIN_CANDLE_STRENGTH = 0.50

# Five-minute reference expiry.
REFERENCE_EXPIRY_MINUTES = 5

# Prevent duplicate signals on same pair/direction.
SIGNAL_LOCK_SECONDS = 300

# Never immediately retry Twelve Data after a 429.
# That would consume/waste more rate-limit time.
HTTP_TIMEOUT = 20


# ============================================================
# MARKET LIST
# ============================================================

MARKETS = [
    ("EURUSD", "EUR/USD", "FX"),
    ("GBPUSD", "GBP/USD", "FX"),
    ("USDJPY", "USD/JPY", "FX"),
    ("AUDUSD", "AUD/USD", "FX"),
    ("NZDUSD", "NZD/USD", "FX"),
    ("USDCAD", "USD/CAD", "FX"),
    ("USDCHF", "USD/CHF", "FX"),
    ("EURGBP", "EUR/GBP", "FX"),
    ("EURJPY", "EUR/JPY", "FX"),
    ("GBPJPY", "GBP/JPY", "FX"),
    ("AUDJPY", "AUD/JPY", "FX"),
    ("EURCHF", "EUR/CHF", "FX"),
    ("EURAUD", "EUR/AUD", "FX"),
    ("GBPAUD", "GBP/AUD", "FX"),
    ("GBPCAD", "GBP/CAD", "FX"),
    ("CADJPY", "CAD/JPY", "FX"),
    ("CHFJPY", "CHF/JPY", "FX"),
    ("AUDCAD", "AUD/CAD", "FX"),
    ("AUDCHF", "AUD/CHF", "FX"),
    ("NZDJPY", "NZD/JPY", "FX"),
]


# ============================================================
# HTTP SESSION
# ============================================================

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "PrecisionScanner/5.1"
})


# ============================================================
# BASIC UTILITIES
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def price_text(price: float) -> str:
    if price >= 100:
        return f"{price:.3f}"
    if price >= 10:
        return f"{price:.4f}"
    return f"{price:.5f}"


def symbol_id(symbol: str) -> str:
    return symbol.replace("/", "")


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle(
    ts: datetime,
    op: float,
    hi: float,
    lo: float,
    cl: float,
    volume: float = 0.0,
) -> dict:
    return {
        "t": ts,
        "open": op,
        "high": hi,
        "low": lo,
        "close": cl,
        "volume": volume,
    }


def parse_td_datetime(value: str) -> datetime:
    value = value.strip()

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def parse_twelve_data(values: list[dict]) -> list[dict]:
    result = []

    for row in values:
        try:
            ts = parse_td_datetime(str(row["datetime"]))

            result.append(
                candle(
                    ts=ts,
                    op=safe_float(row["open"]),
                    hi=safe_float(row["high"]),
                    lo=safe_float(row["low"]),
                    cl=safe_float(row["close"]),
                    volume=safe_float(row.get("volume", 0)),
                )
            )
        except Exception:
            continue

    result.sort(key=lambda x: x["t"])
    return result


def closed_1m_candles(candles: list[dict]) -> list[dict]:
    now = utc_now()
    result = []

    for c in candles:
        # Timestamp represents beginning of 1-minute candle.
        if c["t"] + timedelta(minutes=1) <= now:
            result.append(c)

    return result


def resample_to_5m(candles_1m: list[dict]) -> list[dict]:
    """
    Build closed 5-minute candles locally.

    A five-minute candle is accepted only when all five
    one-minute candles are present.
    """

    buckets: dict[datetime, list[dict]] = {}

    for c in candles_1m:
        ts = c["t"]

        minute = (ts.minute // 5) * 5

        bucket = ts.replace(
            minute=minute,
            second=0,
            microsecond=0,
        )

        buckets.setdefault(bucket, []).append(c)

    result = []

    for bucket, group in buckets.items():
        group.sort(key=lambda x: x["t"])

        if len(group) < 5:
            continue

        expected = [
            bucket + timedelta(minutes=i)
            for i in range(5)
        ]

        actual = [x["t"] for x in group[:5]]

        if actual != expected:
            continue

        first = group[0]
        last = group[4]

        result.append(
            candle(
                ts=bucket,
                op=first["open"],
                hi=max(x["high"] for x in group[:5]),
                lo=min(x["low"] for x in group[:5]),
                cl=last["close"],
                volume=sum(x["volume"] for x in group[:5]),
            )
        )

    result.sort(key=lambda x: x["t"])

    # Make absolutely sure the latest 5M candle is closed.
    now = utc_now()

    result = [
        c for c in result
        if c["t"] + timedelta(minutes=5) <= now
    ]

    return result


# ============================================================
# TWELVE DATA
# ============================================================

def twelve_data_request(symbol: str) -> list[dict]:
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY is not configured."
        )

    params = {
        "symbol": symbol,
        "interval": "1min",
        "outputsize": OUTPUT_SIZE,
        "timezone": "UTC",
        "format": "JSON",
        "apikey": TWELVE_DATA_API_KEY,
    }

    try:
        response = SESSION.get(
            TWELVE_DATA_URL,
            params=params,
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Twelve Data connection error: {exc}"
        )

    if response.status_code == 429:
        print(
            f"[TWELVE] HTTP 429 for {symbol} - "
            "rate limit reached; skipping without retry."
        )
        raise RuntimeError(
            "Twelve Data rate limit reached."
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Twelve Data HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            "Twelve Data returned invalid JSON."
        )

    if data.get("status") == "error":
        raise RuntimeError(
            f"Twelve Data error: "
            f"{data.get('message', 'unknown error')}"
        )

    values = data.get("values")

    if not values:
        raise RuntimeError(
            f"No candle values returned for {symbol}."
        )

    return parse_twelve_data(values)


def get_market_data(symbol: str) -> tuple[list[dict], list[dict]]:
    """
    ONE Twelve Data request.

    Returns:
        1-minute closed candles
        5-minute candles constructed locally
    """

    raw = twelve_data_request(symbol)

    c1 = closed_1m_candles(raw)

    if len(c1) < 120:
        raise RuntimeError(
            f"Not enough closed 1M candles: {len(c1)}"
        )

    c5 = resample_to_5m(c1)

    if len(c5) < 60:
        raise RuntimeError(
            f"Not enough 5M candles after resampling: {len(c5)}"
        )

    latest = c1[-1]["t"]
    age_minutes = (
        utc_now() - latest
    ).total_seconds() / 60.0

    if age_minutes > MAX_FX_DATA_AGE_MINUTES:
        raise RuntimeError(
            f"Data stale: {age_minutes:.1f} minutes old."
        )

    return c1, c5


# ============================================================
# INDICATORS
# ============================================================

def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []

    if len(values) < period:
        return [values[-1]] * len(values)

    multiplier = 2.0 / (period + 1.0)

    result = [values[0]]

    for value in values[1:]:
        result.append(
            (value - result[-1]) * multiplier + result[-1]
        )

    return result


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1)) + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + rs))


def true_ranges(candles: list[dict]) -> list[float]:
    result = []

    for i, c in enumerate(candles):
        if i == 0:
            result.append(c["high"] - c["low"])
            continue

        previous_close = candles[i - 1]["close"]

        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - previous_close),
            abs(c["low"] - previous_close),
        )

        result.append(tr)

    return result


def atr(candles: list[dict], period: int = 14) -> float:
    trs = true_ranges(candles)

    if len(trs) < period:
        return sum(trs) / max(1, len(trs))

    value = sum(trs[:period]) / period

    for tr in trs[period:]:
        value = (
            (value * (period - 1)) + tr
        ) / period

    return value


def macd(values: list[float]) -> tuple[float, float, float]:
    e12 = ema(values, 12)
    e26 = ema(values, 26)

    line = [
        a - b
        for a, b in zip(e12, e26)
    ]

    signal_series = ema(line, 9)

    if not line:
        return 0.0, 0.0, 0.0

    m = line[-1]
    s = signal_series[-1]

    return m, s, m - s


def adx_dmi(
    candles: list[dict],
    period: int = 14,
) -> tuple[float, float, float]:
    if len(candles) < period + 2:
        return 0.0, 0.0, 0.0

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )

        trs.append(tr)

        plus_dm.append(
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus_dm.append(
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

    if len(trs) < period:
        return 0.0, 0.0, 0.0

    atr_value = sum(trs[:period]) / period
    plus_value = sum(plus_dm[:period]) / period
    minus_value = sum(minus_dm[:period]) / period

    dx_values = []
    plus_di = 0.0
    minus_di = 0.0

    for i in range(period, len(trs)):
        atr_value = (
            (atr_value * (period - 1)) + trs[i]
        ) / period

        plus_value = (
            (plus_value * (period - 1)) + plus_dm[i]
        ) / period

        minus_value = (
            (minus_value * (period - 1)) + minus_dm[i]
        ) / period

        if atr_value <= 0:
            continue

        plus_di = 100.0 * plus_value / atr_value
        minus_di = 100.0 * minus_value / atr_value

        denominator = plus_di + minus_di

        if denominator <= 0:
            dx = 0.0
        else:
            dx = (
                100.0
                * abs(plus_di - minus_di)
                / denominator
            )

        dx_values.append(dx)

    if not dx_values:
        return 0.0, plus_di, minus_di

    adx = sum(dx_values[-period:]) / min(
        period,
        len(dx_values),
    )

    return adx, plus_di, minus_di


# ============================================================
# STRUCTURE
# ============================================================

def candle_direction(c: dict) -> int:
    if c["close"] > c["open"]:
        return 1

    if c["close"] < c["open"]:
        return -1

    return 0


def candle_strength(c: dict) -> float:
    rng = c["high"] - c["low"]

    if rng <= 0:
        return 0.0

    body = abs(c["close"] - c["open"])

    return body / rng


def structure_direction(candles: list[dict]) -> int:
    if len(candles) < 10:
        return 0

    recent = candles[-10:]

    highs = [x["high"] for x in recent]
    lows = [x["low"] for x in recent]

    first_high = max(highs[:5])
    second_high = max(highs[5:])

    first_low = min(lows[:5])
    second_low = min(lows[5:])

    if second_high > first_high and second_low > first_low:
        return 1

    if second_high < first_high and second_low < first_low:
        return -1

    return 0


def trend_direction(candles: list[dict]) -> tuple[int, float, float, float]:
    closes = [x["close"] for x in candles]

    e9 = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, 50)[-1]

    if e9 > e21 > e50:
        return 1, e9, e21, e50

    if e9 < e21 < e50:
        return -1, e9, e21, e50

    return 0, e9, e21, e50


def pullback_direction(candles: list[dict]) -> int:
    if len(candles) < 6:
        return 0

    recent = candles[-6:-1]

    bullish = sum(
        1 for c in recent
        if candle_direction(c) == 1
    )

    bearish = sum(
        1 for c in recent
        if candle_direction(c) == -1
    )

    last = candles[-1]
    last_dir = candle_direction(last)

    if last_dir == 1 and bearish >= 2:
        return 1

    if last_dir == -1 and bullish >= 2:
        return -1

    return 0


def room_direction(
    candles: list[dict],
    direction: int,
) -> bool:
    if len(candles) < 20:
        return False

    current = candles[-1]["close"]
    recent = candles[-20:-1]

    if direction == 1:
        resistance = max(x["high"] for x in recent)

        if resistance <= current:
            return True

        distance = resistance - current
        atr_value = atr(candles)

        return distance >= atr_value * 0.75

    if direction == -1:
        support = min(x["low"] for x in recent)

        if support >= current:
            return True

        distance = current - support
        atr_value = atr(candles)

        return distance >= atr_value * 0.75

    return False


# ============================================================
# STRATEGY ANALYSIS
# ============================================================

def analyze_market(
    market_code: str,
    display_symbol: str,
    candles_1m: list[dict],
    candles_5m: list[dict],
) -> dict:

    closes_5 = [x["close"] for x in candles_5m]
    closes_1 = [x["close"] for x in candles_1m]

    last_5 = candles_5m[-1]
    last_1 = candles_1m[-1]

    trend5, e9, e21, e50 = trend_direction(candles_5m)

    structure = structure_direction(candles_5m)

    rsi5 = rsi(closes_5, 14)

    macd_line, macd_signal, macd_hist = macd(closes_5)

    adx, plus_di, minus_di = adx_dmi(candles_5m)

    entry1, _, _, _ = trend_direction(
        candles_1m[-100:]
    )

    pullback = pullback_direction(candles_5m)

    strength = candle_strength(last_1)

    last1_dir = candle_direction(last_1)

    score_call = 0
    score_put = 0

    call_votes = 0
    put_votes = 0

    reasons_call = []
    reasons_put = []

    # --------------------------------------------------------
    # 5M TREND - 20 points
    # --------------------------------------------------------

    if trend5 == 1:
        score_call += 20
        call_votes += 1
        reasons_call.append("5M trend bullish")

    elif trend5 == -1:
        score_put += 20
        put_votes += 1
        reasons_put.append("5M trend bearish")

    # --------------------------------------------------------
    # STRUCTURE - 10 points
    # --------------------------------------------------------

    if structure == 1:
        score_call += 10
        call_votes += 1
        reasons_call.append("structure bullish")

    elif structure == -1:
        score_put += 10
        put_votes += 1
        reasons_put.append("structure bearish")

    # --------------------------------------------------------
    # ADX - 10 points
    # --------------------------------------------------------

    if adx >= MIN_ADX:

        if plus_di > minus_di:
            score_call += 10
            call_votes += 1
            reasons_call.append(
                f"DMI bullish ({adx:.1f} ADX)"
            )

        elif minus_di > plus_di:
            score_put += 10
            put_votes += 1
            reasons_put.append(
                f"DMI bearish ({adx:.1f} ADX)"
            )

    # --------------------------------------------------------
    # MACD - 10 points
    # --------------------------------------------------------

    if macd_line > macd_signal and macd_hist > 0:
        score_call += 10
        call_votes += 1
        reasons_call.append("MACD bullish")

    elif macd_line < macd_signal and macd_hist < 0:
        score_put += 10
        put_votes += 1
        reasons_put.append("MACD bearish")

    # --------------------------------------------------------
    # RSI - 10 points
    # --------------------------------------------------------

    if 50 <= rsi5 <= 68:
        score_call += 10
        call_votes += 1
        reasons_call.append(
            f"RSI bullish ({rsi5:.1f})"
        )

    elif 32 <= rsi5 < 50:
        score_put += 10
        put_votes += 1
        reasons_put.append(
            f"RSI bearish ({rsi5:.1f})"
        )

    # --------------------------------------------------------
    # 1M ENTRY - 15 points
    # --------------------------------------------------------

    if entry1 == 1:
        score_call += 15
        call_votes += 1
        reasons_call.append("1M entry bullish")

    elif entry1 == -1:
        score_put += 15
        put_votes += 1
        reasons_put.append("1M entry bearish")

    # --------------------------------------------------------
    # PULLBACK - 10 points
    # --------------------------------------------------------

    if pullback == 1:
        score_call += 10
        call_votes += 1
        reasons_call.append("clean bullish pullback")

    elif pullback == -1:
        score_put += 10
        put_votes += 1
        reasons_put.append("clean bearish pullback")

    # --------------------------------------------------------
    # CANDLE STRENGTH - 5 points
    # --------------------------------------------------------

    if strength >= MIN_CANDLE_STRENGTH:

        if last1_dir == 1:
            score_call += 5
            call_votes += 1
            reasons_call.append("strong bullish candle")

        elif last1_dir == -1:
            score_put += 5
            put_votes += 1
            reasons_put.append("strong bearish candle")

    # --------------------------------------------------------
    # ROOM - 5 points
    # --------------------------------------------------------

    room_call = room_direction(candles_5m, 1)
    room_put = room_direction(candles_5m, -1)

    if room_call:
        score_call += 5
        reasons_call.append("room above")

    if room_put:
        score_put += 5
        reasons_put.append("room below")

    # --------------------------------------------------------
    # DETERMINE DIRECTION
    # --------------------------------------------------------

    if score_call >= score_put:
        direction = "CALL"
        score = score_call
        dominance = call_votes - put_votes
        reasons = reasons_call
    else:
        direction = "PUT"
        score = score_put
        dominance = put_votes - call_votes
        reasons = reasons_put

    # --------------------------------------------------------
    # REJECTION DIAGNOSTICS
    # --------------------------------------------------------

    rejection = []

    if trend5 == 0:
        rejection.append("5M trend mismatch")

    if structure == 0:
        rejection.append("Structure not aligned")

    if entry1 == 0:
        rejection.append("1M entry mismatch")

    if pullback == 0:
        rejection.append("No clean pullback")

    if adx < MIN_ADX:
        rejection.append("ADX too low")

    if strength < MIN_CANDLE_STRENGTH:
        rejection.append("Confirmation candle weak")

    if last1_dir == 0:
        rejection.append("Confirmation candle mismatch")

    if direction == "CALL" and not room_call:
        rejection.append("Insufficient room")

    if direction == "PUT" and not room_put:
        rejection.append("Insufficient room")

    if dominance < MIN_DOMINANCE:
        rejection.append("Weak directional dominance")

    if direction == "CALL" and entry1 != 1:
        rejection.append("1M entry mismatch")

    if direction == "PUT" and entry1 != -1:
        rejection.append("1M entry mismatch")

    if direction == "CALL" and trend5 != 1:
        rejection.append("5M trend mismatch")

    if direction == "PUT" and trend5 != -1:
        rejection.append("5M trend mismatch")

    qualified = (
        score >= MIN_SCORE
        and dominance >= MIN_DOMINANCE
        and adx >= MIN_ADX
        and strength >= MIN_CANDLE_STRENGTH
        and trend5 != 0
        and entry1 != 0
        and structure != 0
        and pullback != 0
        and last1_dir != 0
    )

    borderline = (
        not qualified
        and score >= 75
        and dominance >= 2
    )

    return {
        "market": market_code,
        "symbol": display_symbol,
        "direction": direction,
        "score": score,
        "dominance": dominance,
        "qualified": qualified,
        "borderline": borderline,
        "price": last1["close"],
        "trend5": trend5,
        "entry1": entry1,
        "structure": structure,
        "pullback": pullback,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "rsi": rsi5,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "candle_strength": strength,
        "last_candle_direction": last1_dir,
        "room_call": room_call,
        "room_put": room_put,
        "rejection": rejection,
        "reasons": reasons,
        "timestamp": last1["t"].isoformat(),
    }


# ============================================================
# TRACKER
# ============================================================

def default_tracker() -> dict:
    return {
        "version": VERSION,
        "created_at": iso_now(),
        "last_scan": None,
        "last_signal": None,
        "scan_count": 0,
        "signal_locks": {},
        "processed_keys": [],
        "delivery_failed_keys": [],
        "signals": {},
        "results": {
            "wins": 0,
            "losses": 0,
        },
        "rejection_reasons": {},
        "rotation_index": 0,
        "provider": "Twelve Data",
        "markets": [
            {
                "code": x[0],
                "symbol": x[1],
                "type": x[2],
            }
            for x in MARKETS
        ],
    }


def load_tracker() -> dict:
    if not TRACKER_FILE.exists():
        return default_tracker()

    try:
        data = json.loads(
            TRACKER_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            return default_tracker()

        base = default_tracker()

        for key, value in data.items():
            base[key] = value

        return base

    except Exception:
        return default_tracker()


TRACKER = load_tracker()


def save_local_tracker() -> None:
    TRACKER_FILE.write_text(
        json.dumps(
            TRACKER,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ============================================================
# GITHUB TRACKER
# ============================================================

def github_save() -> bool:
    if not GITHUB_ACTIONS:
        save_local_tracker()
        return True

    if not GITHUB_TOKEN:
        save_local_tracker()
        return False

    if not GITHUB_REPOSITORY:
        save_local_tracker()
        return False

    save_local_tracker()

    url = (
        "https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{TRACKER_FILE.name}"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        existing = SESSION.get(
            url,
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )

        sha = None

        if existing.status_code == 200:
            sha = existing.json().get("sha")

        import base64

        encoded = base64.b64encode(
            TRACKER_FILE.read_bytes()
        ).decode()

        payload = {
            "message": (
                f"Update {VERSION} tracker "
                f"{utc_now().strftime('%Y-%m-%d %H:%M:%S')} UTC"
            ),
            "content": encoded,
        }

        if sha:
            payload["sha"] = sha

        response = SESSION.put(
            url,
            headers=headers,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        print(
            f"[TRACKER] GitHub save "
            f"{response.status_code}"
        )

        return response.status_code in (200, 201)

    except Exception as exc:
        print(
            f"[TRACKER] GitHub save failed: {exc}"
        )

        return False


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(
            "[TELEGRAM] Not configured."
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = SESSION.post(
            url,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code != 200:
            print(
                "[TELEGRAM] Error:",
                response.text[:500],
            )
            return False

        return True

    except Exception as exc:
        print(
            f"[TELEGRAM] Connection error: {exc}"
        )
        return False


def format_signal(result: dict, signal_id: str) -> str:
    direction = result["direction"]

    emoji = "🟢" if direction == "CALL" else "🔴"

    trend = (
        "BULLISH"
        if result["trend5"] == 1
        else "BEARISH"
        if result["trend5"] == -1
        else "NEUTRAL"
    )

    entry = (
        "BULLISH"
        if result["entry1"] == 1
        else "BEARISH"
        if result["entry1"] == -1
        else "NEUTRAL"
    )

    structure = (
        "BULLISH"
        if result["structure"] == 1
        else "BEARISH"
        if result["structure"] == -1
        else "NEUTRAL"
    )

    return (
        f"{emoji} <b>NEW QUALIFIED SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>{result['symbol']}</b> "
        f"<b>{direction}</b>\n\n"
        f"🎯 <b>Score:</b> "
        f"{result['score']}/100\n"
        f"⏱ <b>Reference expiry:</b> "
        f"{REFERENCE_EXPIRY_MINUTES} minutes\n"
        f"💰 <b>Price:</b> "
        f"{price_text(result['price'])}\n\n"
        f"📊 <b>5M Trend:</b> {trend}\n"
        f"📈 <b>1M Entry:</b> {entry}\n"
        f"🏗 <b>Structure:</b> {structure}\n"
        f"📐 <b>ADX:</b> "
        f"{result['adx']:.1f}\n"
        f"📉 <b>RSI:</b> "
        f"{result['rsi']:.1f}\n"
        f"📊 <b>Dominance:</b> "
        f"{result['dominance']}\n"
        f"💪 <b>Candle strength:</b> "
        f"{result['candle_strength']:.2f}\n\n"
        f"🕐 <b>Signal candle:</b>\n"
        f"{result['timestamp']}\n\n"
        f"🆔 <b>Signal ID:</b>\n"
        f"<code>{signal_id}</code>\n\n"
        f"📡 <b>Feed:</b> Twelve Data\n"
        f"⚠️ Standard market-data reference; "
        f"Pocket Option OTC prices may differ."
    )


# ============================================================
# SIGNAL MANAGEMENT
# ============================================================

def signal_key(result: dict) -> str:
    return (
        f"{result['market']}-"
        f"{result['direction']}-"
        f"{result['timestamp']}"
    )


def signal_id(result: dict) -> str:
    timestamp = int(
        datetime.fromisoformat(
            result["timestamp"]
        ).timestamp()
    )

    return (
        f"{result['market']}-"
        f"{result['direction']}-"
        f"{timestamp}"
    )


def is_locked(result: dict) -> bool:
    key = result["market"]

    lock_until = TRACKER.get(
        "signal_locks",
        {},
    ).get(key)

    if not lock_until:
        return False

    try:
        return float(lock_until) > time.time()
    except Exception:
        return False


def lock_market(result: dict) -> None:
    TRACKER.setdefault(
        "signal_locks",
        {}
    )[result["market"]] = (
        time.time() + SIGNAL_LOCK_SECONDS
    )


def record_signal(
    result: dict,
    sid: str,
    key: str,
) -> None:

    TRACKER["last_signal"] = {
        "signal_id": sid,
        "market": result["market"],
        "symbol": result["symbol"],
        "direction": result["direction"],
        "score": result["score"],
        "price": result["price"],
        "created_at": iso_now(),
    }

    TRACKER.setdefault(
        "signals",
        {}
    )[sid] = {
        "signal_id": sid,
        "market": result["market"],
        "symbol": result["symbol"],
        "direction": result["direction"],
        "score": result["score"],
        "dominance": result["dominance"],
        "price": result["price"],
        "timestamp": result["timestamp"],
        "created_at": iso_now(),
        "result": "PENDING",
        "provider": "Twelve Data",
    }

    TRACKER.setdefault(
        "processed_keys",
        []
    ).append(key)

    # Keep tracker from growing forever.
    TRACKER["processed_keys"] = (
        TRACKER["processed_keys"][-1000:]
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def telegram_get_updates() -> list[dict]:
    if not TELEGRAM_TOKEN:
        return []

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/getUpdates"
    )

    offset = TRACKER.get(
        "telegram_offset",
        0,
    )

    params = {
        "timeout": 1,
        "offset": offset,
    }

    try:
        response = SESSION.get(
            url,
            params=params,
            timeout=5,
        )

        if response.status_code != 200:
            return []

        data = response.json()

        return data.get("result", [])

    except Exception:
        return []


def telegram_commands() -> None:
    updates = telegram_get_updates()

    if not updates:
        return

    for update in updates:

        update_id = update.get("update_id")

        if update_id is not None:
            TRACKER["telegram_offset"] = (
                update_id + 1
            )

        message = update.get("message", {})
        text = str(
            message.get("text", "")
        ).strip()

        if not text:
            continue

        if text == "/start":
            telegram_send(
                "🧠 <b>Precision Scanner V5.1</b>\n\n"
                "Commands:\n"
                "/scan - scan current rotation\n"
                "/stats - show tracker stats\n"
                "/markets - show markets\n"
                "/win SIGNAL_ID - record WIN\n"
                "/loss SIGNAL_ID - record LOSS"
            )

        elif text == "/help":
            telegram_send(
                "🧠 <b>V5.1 Commands</b>\n\n"
                "/scan\n"
                "/stats\n"
                "/markets\n"
                "/win SIGNAL_ID\n"
                "/loss SIGNAL_ID"
            )

        elif text == "/markets":
            lines = [
                f"{i + 1:02d}. {symbol}"
                for i, (_, symbol, _) in enumerate(MARKETS)
            ]

            telegram_send(
                "📊 <b>V5.1 Markets</b>\n\n"
                + "\n".join(lines)
            )

        elif text == "/stats":
            wins = TRACKER.get(
                "results",
                {}
            ).get("wins", 0)

            losses = TRACKER.get(
                "results",
                {}
            ).get("losses", 0)

            total = wins + losses

            if total:
                rate = (
                    wins / total
                ) * 100
            else:
                rate = 0.0

            telegram_send(
                "📊 <b>Scanner Stats</b>\n\n"
                f"WIN: {wins}\n"
                f"LOSS: {losses}\n"
                f"Recorded: {total}\n"
                f"Result rate: {rate:.1f}%\n\n"
                f"Scanner: {VERSION}\n"
                f"Provider: Twelve Data"
            )

        elif text == "/scan":
            telegram_send(
                "🔎 Manual scan request received.\n"
                "The next scan cycle will process the "
                "current market rotation."
            )

        elif text.startswith("/win "):
            sid = text.split(
                maxsplit=1
            )[1].strip()

            record_outcome(sid, "WIN")

        elif text.startswith("/loss "):
            sid = text.split(
                maxsplit=1
            )[1].strip()

            record_outcome(sid, "LOSS")

    github_save()


def record_outcome(
    sid: str,
    outcome: str,
) -> None:

    signal = TRACKER.setdefault(
        "signals",
        {}
    ).get(sid)

    if not signal:
        telegram_send(
            f"❌ Signal not found:\n<code>{sid}</code>"
        )
        return

    previous = signal.get("result")

    if previous == outcome:
        telegram_send(
            f"ℹ️ Signal already recorded as "
            f"<b>{outcome}</b>."
        )
        return

    if previous == "WIN":
        TRACKER["results"]["wins"] = max(
            0,
            TRACKER["results"]["wins"] - 1,
        )

    elif previous == "LOSS":
        TRACKER["results"]["losses"] = max(
            0,
            TRACKER["results"]["losses"] - 1,
        )

    signal["result"] = outcome
    signal["result_time"] = iso_now()

    if outcome == "WIN":
        TRACKER["results"]["wins"] += 1
    else:
        TRACKER["results"]["losses"] += 1

    telegram_send(
        f"✅ <b>{outcome} RECORDED</b>\n\n"
        f"Signal:\n<code>{sid}</code>"
    )

    github_save()


# ============================================================
# ROTATION
# ============================================================

def get_rotation_markets() -> list[tuple[str, str, str]]:
    total = len(MARKETS)

    start = int(
        TRACKER.get(
            "rotation_index",
            0,
        )
    ) % total

    selected = []

    for i in range(
        min(MARKETS_PER_CYCLE, total)
    ):
        selected.append(
            MARKETS[
                (start + i) % total
            ]
        )

    TRACKER["rotation_index"] = (
        start + len(selected)
    ) % total

    return selected


# ============================================================
# SCAN
# ============================================================

def scan_cycle() -> dict:

    print()
    print(
        f"=== {APP_NAME} ONE-SHOT ==="
    )

    selected = get_rotation_markets()

    print(
        f"[ROTATION] Processing "
        f"{len(selected)} markets this cycle"
    )

    for code, symbol, market_type in selected:
        print(
            f"  -> {code} ({symbol})"
        )

    summary = {
        "qualified": 0,
        "borderline": 0,
        "rejected": 0,
        "errors": 0,
        "new": 0,
        "alerts": 0,
        "duplicates": 0,
        "locked": 0,
        "markets": len(MARKETS),
        "cycle_markets": len(selected),
        "fx": len(MARKETS),
        "crypto": 0,
    }

    rejection_counter = Counter()

    for code, symbol, market_type in selected:

        print()
        print(
            f"[SCAN] {code} ({symbol}) "
            f"type={market_type}"
        )

        try:
            c1, c5 = get_market_data(symbol)

            print(
                f"[DATA] OK {symbol}: "
                f"1 request -> "
                f"{len(c1)} x 1M + "
                f"{len(c5)} x 5M local"
            )

            result = analyze_market(
                code,
                symbol,
                c1,
                c5,
            )

            if result["qualified"]:
                summary["qualified"] += 1

                key = signal_key(result)
                sid = signal_id(result)

                if key in TRACKER.get(
                    "processed_keys",
                    [],
                ):
                    summary["duplicates"] += 1

                    print(
                        f"[DUPLICATE] {sid}"
                    )

                    continue

                if is_locked(result):
                    summary["locked"] += 1

                    print(
                        f"[LOCKED] {symbol}"
                    )

                    continue

                record_signal(
                    result,
                    sid,
                    key,
                )

                lock_market(result)

                message = format_signal(
                    result,
                    sid,
                )

                delivered = telegram_send(
                    message
                )

                if delivered:
                    summary["alerts"] += 1
                    print(
                        f"[SIGNAL] SENT {sid}"
                    )
                else:
                    TRACKER.setdefault(
                        "delivery_failed_keys",
                        []
                    ).append(key)

                    print(
                        f"[SIGNAL] "
                        f"Telegram delivery failed "
                        f"for {sid}"
                    )

                summary["new"] += 1

            elif result["borderline"]:
                summary["borderline"] += 1

                print(
                    f"[BORDERLINE] "
                    f"{symbol} "
                    f"{result['direction']} "
                    f"score={result['score']} "
                    f"dom={result['dominance']}"
                )

            else:
                summary["rejected"] += 1

                for reason in result[
                    "rejection"
                ]:
                    rejection_counter[
                        reason
                    ] += 1

        except Exception as exc:
            summary["errors"] += 1

            print(
                f"[ERROR] {symbol}: {exc}"
            )

    TRACKER["last_scan"] = iso_now()

    TRACKER["scan_count"] = (
        int(
            TRACKER.get(
                "scan_count",
                0,
            )
        ) + 1
    )

    existing_rejections = TRACKER.setdefault(
        "rejection_reasons",
        {}
    )

    for reason, count in rejection_counter.items():
        existing_rejections[reason] = (
            existing_rejections.get(
                reason,
                0,
            ) + count
        )

    print()
    print(
        "[REJECTION DIAGNOSTICS]"
    )

    for reason, count in rejection_counter.most_common():
        print(
            f"  {count:03d} {reason}"
        )

    print()
    print(
        "[SUMMARY]",
        json.dumps(
            summary,
            indent=2,
        ),
    )

    save_local_tracker()
    github_save()

    return summary


# ============================================================
# STARTUP
# ============================================================

def print_markets() -> None:
    print()
    print(
        "[MARKETS] V5.1 ROTATING LIST"
    )

    for i, (_, symbol, market_type) in enumerate(
        MARKETS,
        start=1,
    ):
        print(
            f"  {i:02d}. "
            f"{market_type:<4} "
            f"{symbol}"
        )

    print(
        f"[MARKETS] Total: {len(MARKETS)}"
    )

    print(
        f"[MARKETS] Per cycle: "
        f"{MARKETS_PER_CYCLE}"
    )

    print(
        "[MARKETS] "
        "Full rotation approximately "
        f"{math.ceil(len(MARKETS) / MARKETS_PER_CYCLE)} cycles"
    )


def validate_configuration() -> bool:

    if not TWELVE_DATA_API_KEY:
        print()
        print(
            "[ERROR] "
            "TWELVE_DATA_API_KEY is not configured."
        )

        return False

    print(
        f"[CONFIG] Twelve Data API: configured"
    )

    print(
        f"[CONFIG] Telegram: "
        f"{'configured' if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else 'not configured'}"
    )

    print(
        f"[CONFIG] GitHub tracker: "
        f"{'enabled' if GITHUB_ACTIONS and GITHUB_TOKEN else 'local'}"
    )

    return True


# ============================================================
# MAIN
# ============================================================

def run_once() -> None:

    print(
        f"[START] {VERSION} "
        "Twelve Data market data"
    )

    print(
        f"[CONFIG] Markets: {len(MARKETS)} "
        f"Per cycle: {MARKETS_PER_CYCLE} "
        f"Interval: {SCAN_INTERVAL}s"
    )

    if not validate_configuration():
        sys.exit(1)

    print_markets()

    telegram_commands()

    scan_cycle()


def run_loop() -> None:

    print(
        f"[START] {VERSION} LOOP MODE"
    )

    print(
        f"[CONFIG] "
        f"{len(MARKETS)} markets "
        f"/ {MARKETS_PER_CYCLE} per cycle "
        f"/ {SCAN_INTERVAL}s interval"
    )

    if not validate_configuration():
        sys.exit(1)

    print_markets()

    while True:

        cycle_start = time.time()

        try:
            telegram_commands()
            scan_cycle()

        except KeyboardInterrupt:
            print(
                "\n[STOP] Scanner stopped."
            )
            break

        except Exception as exc:
            print(
                f"[LOOP ERROR] {exc}"
            )

        elapsed = time.time() - cycle_start

        sleep_for = max(
            1,
            SCAN_INTERVAL - int(elapsed),
        )

        print()
        print(
            f"[WAIT] Next cycle in "
            f"{sleep_for}s"
        )

        time.sleep(sleep_for)


def main() -> None:

    parser = argparse.ArgumentParser(
        description=APP_NAME
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan cycle.",
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously.",
    )

    args = parser.parse_args()

    if args.loop:
        run_loop()
        return

    # Default to one-shot.
    run_once()


if __name__ == "__main__":
    main()
