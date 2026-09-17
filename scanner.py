#!/usr/bin/env python3
"""
PRECISION SCANNER V5.0
Twelve Data Multi-Market Scanner
--------------------------------

Market data:
    Twelve Data

Strategy:
    5M trend + 1M entry
    EMA 9/21/50
    RSI
    MACD
    ADX/DMI
    Structure
    Pullback
    Confirmation candle
    Room
    Extension
    Directional dominance

Markets:
    Forex majors
    Forex minors/crosses
    Crypto

Telegram:
    Qualified CALL / PUT alerts

Tracker:
    tracker.json
    Optional GitHub Actions persistence

IMPORTANT:
    This is a research/test scanner.
    It does NOT execute trades.
    It does NOT guarantee profitability.

Pocket Option note:
    Normal forex data can be directionally compared with
    Pocket Option standard forex markets.

    Pocket Option OTC markets may use a different/internal
    price feed, so exact candle-by-candle synchronization
    with OTC is NOT guaranteed.
"""

from __future__ import annotations

import base64
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests


# ============================================================
# VERSION
# ============================================================

VERSION = "V5.0"


# ============================================================
# TWELVE DATA
# ============================================================

TWELVE_API = "https://api.twelvedata.com"

TWELVE_KEY = os.getenv(
    "TWELVE_DATA_API_KEY",
    ""
).strip()

TIMEOUT = 20
RETRIES = 3


# ============================================================
# TIMEFRAMES
# ============================================================

TF5 = "5min"
TF1 = "1min"

# Reference expiry only
EXPIRY = 5


# ============================================================
# SCANNER SETTINGS
# ============================================================

MIN_SCORE = 85
BORDERLINE = 80

MIN_DOM = 3
MIN_ADX = 18
MIN_CANDLE = 0.50

CALL_RSI = (43, 68)
PUT_RSI = (32, 57)

# Signal lock
LOCK = 300

# Number of candles requested
LIMIT = 220

# Number of markets
MIN_SCAN_SYMBOLS = 10
MAX_SCAN_SYMBOLS = 20


# ============================================================
# SCAN INTERVAL
# ============================================================

# Scan every 60 seconds.
#
# This is intentionally faster than V4's 300 seconds because
# the scanner uses 1-minute entry candles.
#
# The strategy still uses a 5-minute reference trend.
SCAN_INTERVAL = 60


# ============================================================
# FOREX MARKETS
# ============================================================

FOREX_PAIRS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "AUD/USD",
    "NZD/USD",
    "USD/CAD",
    "USD/CHF",

    "EUR/GBP",
    "EUR/JPY",
    "GBP/JPY",

    "AUD/JPY",
    "EUR/CHF",
    "EUR/AUD",
    "GBP/AUD",
    "GBP/CAD",
    "CAD/JPY",
    "CHF/JPY",

    "AUD/CAD",
    "AUD/CHF",
    "NZD/JPY",
    "NZD/CHF",
    "NZD/CAD",

    "EUR/NZD",
    "GBP/NZD",
]


# ============================================================
# CRYPTO MARKETS
# ============================================================

CRYPTO_PAIRS = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
    "DOGE/USD",
    "BNB/USD",
    "ADA/USD",
    "AVAX/USD",
    "LINK/USD",
    "LTC/USD",
    "DOT/USD",
    "TRX/USD",
    "SUI/USD",
    "BCH/USD",
    "TON/USD",
    "NEAR/USD",
    "UNI/USD",
]


# ============================================================
# TRACKER
# ============================================================

TRACKER_FILE = "tracker.json"
MAX_ITEMS = 1000


# ============================================================
# TELEGRAM
# ============================================================

TG = os.getenv(
    "TELEGRAM_TOKEN",
    ""
).strip()

CHAT = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
).strip()


# ============================================================
# GITHUB
# ============================================================

GH = os.getenv(
    "GITHUB_TOKEN",
    ""
).strip()

REPO = os.getenv(
    "GITHUB_REPOSITORY",
    ""
).strip()

ACTIONS = (
    os.getenv(
        "GITHUB_ACTIONS",
        ""
    ).lower()
    == "true"
)


# ============================================================
# HTTP SESSION
# ============================================================

S = requests.Session()

S.headers.update(
    {
        "User-Agent":
            f"PrecisionScanner/{VERSION}",
        "Accept":
            "application/json",
    }
)


# ============================================================
# GLOBALS
# ============================================================

MARKETS: list[dict[str, Any]] = []


# ============================================================
# BASIC HELPERS
# ============================================================

def now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def unix():
    return int(time.time())


def f(x):
    try:
        y = float(x)

        if math.isfinite(y):
            return y

        return None

    except Exception:
        return None


def price(x):

    if x is None:
        return "N/A"

    x = float(x)

    if abs(x) >= 100:
        return f"{x:.3f}"

    if abs(x) >= 1:
        return f"{x:.5f}"

    return f"{x:.8f}"


# ============================================================
# DEFAULT TRACKER
# ============================================================

def default_tracker():

    return {
        "version": VERSION,

        "signals": [],

        "borderline": [],

        "metadata": {
            "last_scan": None,
            "last_signal": None,

            "scan_count": 0,

            "signal_locks": {},

            "processed_keys": [],

            "delivery_failed_keys": [],

            "telegram_offset": None,

            "markets": [],

            "last_rejection_reasons": {},

            "data_provider": "Twelve Data",
        },
    }


# ============================================================
# LOAD TRACKER
# ============================================================

def load_tracker():

    try:

        with open(
            TRACKER_FILE,
            encoding="utf8",
        ) as h:

            d = json.load(h)

        b = default_tracker()

        if "signals" in d:
            b["signals"] = d["signals"]

        if "borderline" in d:
            b["borderline"] = d["borderline"]

        b["metadata"].update(
            d.get(
                "metadata",
                {}
            )
        )

        return b

    except Exception:

        return default_tracker()


T = load_tracker()


# ============================================================
# SAVE TRACKER
# ============================================================

def save_local():

    with open(
        TRACKER_FILE,
        "w",
        encoding="utf8",
    ) as h:

        json.dump(
            T,
            h,
            indent=2,
            ensure_ascii=False,
        )


# ============================================================
# GITHUB SAVE
# ============================================================

def gh_save():

    save_local()

    if (
        not ACTIONS
        or not GH
        or not REPO
    ):
        return False

    url = (
        f"https://api.github.com/"
        f"repos/{REPO}/contents/"
        f"{TRACKER_FILE}"
    )

    headers = {
        "Authorization":
            f"Bearer {GH}",

        "Accept":
            "application/vnd.github+json",

        "X-GitHub-Api-Version":
            "2022-11-28",

        "User-Agent":
            f"PrecisionScanner/{VERSION}",
    }

    try:

        r = S.get(
            url,
            headers=headers,
            timeout=TIMEOUT,
        )

        sha = (
            r.json().get("sha")
            if r.status_code == 200
            else None
        )

        if r.status_code not in (
            200,
            404,
        ):

            print(
                "[TRACKER] GET",
                r.status_code,
                r.text[:300],
            )

            return False

        raw = json.dumps(
            T,
            indent=2,
            ensure_ascii=False,
        ).encode()

        payload = {
            "message":
                f"Update tracker {VERSION}",

            "content":
                base64.b64encode(
                    raw
                ).decode(),
        }

        if sha:
            payload["sha"] = sha

        r = S.put(
            url,
            headers=headers,
            json=payload,
            timeout=TIMEOUT,
        )

        print(
            "[TRACKER] GitHub save",
            r.status_code,
        )

        return r.status_code in (
            200,
            201,
        )

    except Exception as e:

        print(
            "[TRACKER] save error",
            e,
        )

        return False


# ============================================================
# TELEGRAM
# ============================================================

def tg(
    method,
    **kwargs,
):

    if not TG:
        return None

    try:

        return S.post(
            f"https://api.telegram.org/"
            f"bot{TG}/{method}",

            timeout=TIMEOUT + 5,

            **kwargs,
        )

    except Exception as e:

        print(
            "[TELEGRAM]",
            e,
        )

        return None


def send(text):

    if not TG or not CHAT:

        print(
            "[TELEGRAM] credentials missing"
        )

        return False

    r = tg(
        "sendMessage",

        json={
            "chat_id": CHAT,

            "text":
                text[:3900],

            "disable_web_page_preview":
                True,
        },
    )

    ok = bool(
        r
        and r.status_code == 200
    )

    if not ok:

        print(
            "[TELEGRAM] send failed",

            r.status_code
            if r
            else "exception",

            r.text[:300]
            if r
            else "",
        )

    return ok


def updates():

    if not TG:
        return []

    params = {
        "timeout": 1,
        "limit": 20,
    }

    offset = (
        T["metadata"]
        .get("telegram_offset")
    )

    if offset is not None:
        params["offset"] = offset

    r = tg(
        "getUpdates",
        params=params,
    )

    if (
        not r
        or r.status_code != 200
    ):
        return []

    try:

        return r.json().get(
            "result",
            [],
        )

    except Exception:

        return []


# ============================================================
# TWELVE DATA API
# ============================================================

def twelve_get(
    endpoint,
    params,
):

    if not TWELVE_KEY:

        raise RuntimeError(
            "TWELVE_DATA_API_KEY "
            "is missing"
        )

    p = dict(params)

    p["apikey"] = TWELVE_KEY

    last_error = "unknown"

    for attempt in range(
        1,
        RETRIES + 1,
    ):

        try:

            r = S.get(
                TWELVE_API + endpoint,
                params=p,
                timeout=TIMEOUT,
            )

            if r.status_code != 200:

                last_error = (
                    f"HTTP {r.status_code}: "
                    f"{r.text[:300]}"
                )

                print(
                    "[TWELVE]",
                    last_error,
                )

                if r.status_code in (
                    400,
                    401,
                    403,
                    404,
                ):
                    break

                time.sleep(
                    attempt * 2
                )

                continue

            data = r.json()

            if (
                isinstance(data, dict)
                and (
                    data.get("status")
                    == "error"
                    or data.get("code")
                )
            ):

                last_error = (
                    f"{data.get('code', '')} "
                    f"{data.get('message', '')}"
                )

                print(
                    "[TWELVE]",
                    last_error,
                )

                break

            return data

        except Exception as e:

            last_error = str(e)

            print(
                "[TWELVE] exception",
                attempt,
                "/",
                RETRIES,
                e,
            )

            time.sleep(
                min(
                    attempt * 2,
                    5,
                )
            )

    raise RuntimeError(
        f"Twelve Data failed: "
        f"{last_error}"
    )


# ============================================================
# DATA VALIDATION
# ============================================================

def validate_series(data):

    if not isinstance(
        data,
        dict,
    ):

        raise RuntimeError(
            "Invalid Twelve Data response"
        )

    values = data.get(
        "values"
    )

    if not values:

        message = (
            data.get("message")
            or data.get("status")
            or "No values returned"
        )

        raise RuntimeError(
            str(message)
        )

    return values


# ============================================================
# CANDLE CONVERSION
# ============================================================

def candles(values):

    out = []

    for row in values:

        try:

            o = f(
                row.get("open")
            )

            h = f(
                row.get("high")
            )

            l = f(
                row.get("low")
            )

            c = f(
                row.get("close")
            )

            dt = str(
                row.get("datetime")
                or ""
            )

            if None in (
                o,
                h,
                l,
                c,
            ):
                continue

            if h < l:
                continue

            # Twelve Data forex timestamps
            # are normally UTC when timezone
            # is explicitly requested.
            try:

                dt2 = (
                    dt.replace(
                        "Z",
                        "+00:00",
                    )
                )

                timestamp = (
                    datetime.fromisoformat(
                        dt2
                    ).timestamp()
                )

            except Exception:

                timestamp = 0

            out.append(
                {
                    "t": timestamp,
                    "o": o,
                    "h": h,
                    "l": l,
                    "c": c,
                }
            )

        except Exception:
            continue

    out.sort(
        key=lambda x: x["t"]
    )

    return out


# ============================================================
# FETCH CANDLES
# ============================================================

def fetch(
    symbol,
    interval,
):

    data = twelve_get(
        "/time_series",
        {
            "symbol":
                symbol,

            "interval":
                interval,

            "outputsize":
                LIMIT,

            "timezone":
                "UTC",

            "format":
                "JSON",
        },
    )

    values = validate_series(
        data
    )

    c = candles(
        values
    )

    if not c:

        raise RuntimeError(
            f"No candles returned "
            f"for {symbol} "
            f"{interval}"
        )

    print(
        f"[DATA] OK {symbol} "
        f"{interval}: "
        f"{len(c)} candles"
    )

    return c


# ============================================================
# EMA
# ============================================================

def ema(a, n):

    if len(a) < n:
        return [None] * len(a)

    z = [None] * len(a)

    prev = sum(
        a[:n]
    ) / n

    z[n - 1] = prev

    k = 2 / (
        n + 1
    )

    for i in range(
        n,
        len(a),
    ):

        prev = (
            (a[i] - prev)
            * k
            + prev
        )

        z[i] = prev

    return z


# ============================================================
# RSI
# ============================================================

def rsi(
    a,
    n=14,
):

    z = [None] * len(a)

    if len(a) <= n:
        return z

    g = (
        sum(
            max(
                a[i] - a[i - 1],
                0,
            )
            for i in range(
                1,
                n + 1,
            )
        )
        / n
    )

    l = (
        sum(
            max(
                a[i - 1] - a[i],
                0,
            )
            for i in range(
                1,
                n + 1,
            )
        )
        / n
    )

    def q(gain, loss):

        if loss == 0:
            return 100

        return (
            100
            - 100
            / (
                1
                + gain / loss
            )
        )

    z[n] = q(
        g,
        l,
    )

    for i in range(
        n + 1,
        len(a),
    ):

        d = (
            a[i]
            - a[i - 1]
        )

        g = (
            (
                g * (n - 1)
            )
            + max(
                d,
                0,
            )
        ) / n

        l = (
            (
                l * (n - 1)
            )
            + max(
                -d,
                0,
            )
        ) / n

        z[i] = q(
            g,
            l,
        )

    return z


# ============================================================
# ATR
# ============================================================

def atr(
    c,
    n=14,
):

    if len(c) <= n:
        return [None] * len(c)

    tr = [0]

    for i in range(
        1,
        len(c),
    ):

        tr.append(
            max(
                c[i]["h"]
                - c[i]["l"],

                abs(
                    c[i]["h"]
                    - c[i - 1]["c"]
                ),

                abs(
                    c[i]["l"]
                    - c[i - 1]["c"]
                ),
            )
        )

    z = [None] * len(c)

    p = (
        sum(
            tr[1:n + 1]
        )
        / n
    )

    z[n] = p

    for i in range(
        n + 1,
        len(c),
    ):

        p = (
            (
                p * (n - 1)
            )
            + tr[i]
        ) / n

        z[i] = p

    return z


# ============================================================
# MACD
# ============================================================

def macd(a):

    e12 = ema(
        a,
        12,
    )

    e26 = ema(
        a,
        26,
    )

    m = [None] * len(a)

    idx = []
    vals = []

    for i in range(
        len(a)
    ):

        if (
            e12[i]
            is not None
            and
            e26[i]
            is not None
        ):

            m[i] = (
                e12[i]
                - e26[i]
            )

            idx.append(i)
            vals.append(m[i])

    se = ema(
        vals,
        9,
    )

    sig = [None] * len(a)
    hist = [None] * len(a)

    for j, i in enumerate(idx):

        sig[i] = se[j]

        if sig[i] is not None:

            hist[i] = (
                m[i]
                - sig[i]
            )

    return hist


# ============================================================
# ADX / DMI
# ============================================================

def adx(
    c,
    n=14,
):

    L = len(c)

    tr = [0] * L
    pd = [0] * L
    md = [0] * L

    for i in range(
        1,
        L,
    ):

        tr[i] = max(
            c[i]["h"]
            - c[i]["l"],

            abs(
                c[i]["h"]
                - c[i - 1]["c"]
            ),

            abs(
                c[i]["l"]
                - c[i - 1]["c"]
            ),
        )

        u = (
            c[i]["h"]
            - c[i - 1]["h"]
        )

        d = (
            c[i - 1]["l"]
            - c[i]["l"]
        )

        pd[i] = (
            u
            if (
                u > d
                and u > 0
            )
            else 0
        )

        md[i] = (
            d
            if (
                d > u
                and d > 0
            )
            else 0
        )

    z = [None] * L
    plus = [None] * L
    minus = [None] * L
    dx = [None] * L

    if L <= 2 * n:
        return (
            z,
            plus,
            minus,
        )

    at = (
        sum(
            tr[1:n + 1]
        )
        / n
    )

    pp = (
        sum(
            pd[1:n + 1]
        )
        / n
    )

    mm = (
        sum(
            md[1:n + 1]
        )
        / n
    )

    for i in range(
        n,
        L,
    ):

        if i > n:

            at = (
                (
                    at
                    * (n - 1)
                )
                + tr[i]
            ) / n

            pp = (
                (
                    pp
                    * (n - 1)
                )
                + pd[i]
            ) / n

            mm = (
                (
                    mm
                    * (n - 1)
                )
                + md[i]
            ) / n

        if at:

            plus[i] = (
                100
                * pp
                / at
            )

            minus[i] = (
                100
                * mm
                / at
            )

            den = (
                plus[i]
                + minus[i]
            )

            if den:

                dx[i] = (
                    100
                    * abs(
                        plus[i]
                        - minus[i]
                    )
                    / den
                )

    count = 0
    start = None
    seed = 0

    for i in range(
        n,
        L,
    ):

        if dx[i] is not None:

            seed += dx[i]
            count += 1

            if count == n:

                start = i
                break

    if start is None:

        return (
            z,
            plus,
            minus,
        )

    p = (
        seed
        / n
    )

    z[start] = p

    for i in range(
        start + 1,
        L,
    ):

        if dx[i] is not None:

            p = (
                (
                    p
                    * (n - 1)
                )
                + dx[i]
            ) / n

            z[i] = p

    return (
        z,
        plus,
        minus,
    )


# ============================================================
# TREND
# ============================================================

def trend(c):

    a = [
        x["c"]
        for x in c
    ]

    e9 = ema(
        a,
        9,
    )[-1]

    e21 = ema(
        a,
        21,
    )[-1]

    e50 = ema(
        a,
        50,
    )[-1]

    if None in (
        e9,
        e21,
        e50,
    ):

        return "NEUTRAL"

    if (
        e9 > e21 > e50
        or (
            e9 > e21
            and a[-1] > e21
        )
    ):

        return "BULLISH"

    if (
        e9 < e21 < e50
        or (
            e9 < e21
            and a[-1] < e21
        )
    ):

        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# STRUCTURE
# ============================================================

def structure(
    c,
    n=8,
):

    if len(c) < n + 2:
        return "NEUTRAL"

    a = c[-n:]

    h1 = max(
        x["h"]
        for x in a[: n // 2]
    )

    h2 = max(
        x["h"]
        for x in a[n // 2:]
    )

    l1 = min(
        x["l"]
        for x in a[: n // 2]
    )

    l2 = min(
        x["l"]
        for x in a[n // 2:]
    )

    if (
        h2 > h1
        and l2 > l1
    ):

        return "BULLISH"

    if (
        h2 < h1
        and l2 < l1
    ):

        return "BEARISH"

    if (
        a[-1]["c"]
        > a[0]["c"]
    ):

        return "BULLISH"

    if (
        a[-1]["c"]
        < a[0]["c"]
    ):

        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# CANDLE HELPERS
# ============================================================

def strength(x):

    r = (
        x["h"]
        - x["l"]
    )

    if r <= 0:
        return 0

    return abs(
        x["c"]
        - x["o"]
    ) / r


def cdir(x):

    if x["c"] > x["o"]:
        return "BULLISH"

    if x["c"] < x["o"]:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# ANALYSIS
# ============================================================

def analyze(
    market,
):

    name = market["name"]
    symbol = market["symbol"]
    market_type = market["type"]

    print(
        f"\n[SCAN] {name} "
        f"({symbol}) "
        f"type={market_type}"
    )

    c5 = fetch(
        symbol,
        TF5,
    )

    c1 = fetch(
        symbol,
        TF1,
    )

    if (
        len(c5) < 60
        or len(c1) < 60
    ):

        raise RuntimeError(
            "insufficient candles"
        )

    a5 = [
        x["c"]
        for x in c5
    ]

    tr5 = trend(
        c5
    )

    en1 = trend(
        c1
    )

    st = structure(
        c5
    )

    rv = rsi(
        a5
    )[-1]

    mh = macd(
        a5
    )[-1]

    av, pdi, mdi = adx(
        c5
    )

    ax = av[-1]
    pi = pdi[-1]
    mi = mdi[-1]

    ec = c1[-1]

    cs = strength(
        ec
    )

    votes = {
        "CALL": 0,
        "PUT": 0,
    }

    for d, pts in (
        (tr5, 4),
        (en1, 4),
        (st, 3),
    ):

        if d == "BULLISH":

            votes["CALL"] += pts

        elif d == "BEARISH":

            votes["PUT"] += pts

    if (
        pi is not None
        and mi is not None
    ):

        if pi > mi:
            votes["CALL"] += 2

        elif mi > pi:
            votes["PUT"] += 2

    if mh is not None:

        if mh > 0:
            votes["CALL"] += 2

        elif mh < 0:
            votes["PUT"] += 2

    if rv is not None:

        if (
            CALL_RSI[0]
            <= rv
            <= CALL_RSI[1]
        ):

            votes["CALL"] += 1

        if (
            PUT_RSI[0]
            <= rv
            <= PUT_RSI[1]
        ):

            votes["PUT"] += 1

    if (
        votes["CALL"]
        == votes["PUT"]
    ):

        direction = "NO TRADE"
        dom = 0

    else:

        direction = (
            "CALL"
            if votes["CALL"]
            > votes["PUT"]
            else "PUT"
        )

        dom = abs(
            votes["CALL"]
            - votes["PUT"]
        )

    blockers = []

    sc = {
        "Trend": 0,
        "Structure": 0,
        "ADX/DMI": 0,
        "MACD": 0,
        "RSI": 0,
        "Entry": 0,
        "Pullback": 0,
        "Candle": 0,
        "Room": 0,
        "Extension": 0,
    }

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    if (
        (
            direction == "CALL"
            and tr5 == "BULLISH"
        )
        or
        (
            direction == "PUT"
            and tr5 == "BEARISH"
        )
    ):

        sc["Trend"] = 20

    else:

        blockers.append(
            "5M trend mismatch"
        )

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    if (
        (
            direction == "CALL"
            and st == "BULLISH"
        )
        or
        (
            direction == "PUT"
            and st == "BEARISH"
        )
    ):

        sc["Structure"] = 10

    else:

        blockers.append(
            "Structure not aligned"
        )

    # --------------------------------------------------------
    # ADX / DMI
    # --------------------------------------------------------

    if ax is None:

        blockers.append(
            "ADX unavailable"
        )

    elif ax < MIN_ADX:

        blockers.append(
            "ADX too low"
        )

    else:

        sc["ADX/DMI"] = 10

        if (
            pi is None
            or mi is None
        ):

            blockers.append(
                "DMI unavailable"
            )

        elif not (
            (
                direction == "CALL"
                and pi > mi
            )
            or
            (
                direction == "PUT"
                and mi > pi
            )
        ):

            blockers.append(
                "DMI mismatch"
            )

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    if mh is None:

        blockers.append(
            "MACD unavailable"
        )

    elif (
        (
            direction == "CALL"
            and mh > 0
        )
        or
        (
            direction == "PUT"
            and mh < 0
        )
    ):

        sc["MACD"] = 10

    else:

        blockers.append(
            "MACD mismatch"
        )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if rv is None:

        blockers.append(
            "RSI unavailable"
        )

    elif (
        (
            direction == "CALL"
            and CALL_RSI[0]
            <= rv
            <= CALL_RSI[1]
        )
        or
        (
            direction == "PUT"
            and PUT_RSI[0]
            <= rv
            <= PUT_RSI[1]
        )
    ):

        sc["RSI"] = 10

    else:

        blockers.append(
            "RSI outside zone"
        )

    # --------------------------------------------------------
    # 1M ENTRY
    # --------------------------------------------------------

    if (
        (
            direction == "CALL"
            and en1 == "BULLISH"
        )
        or
        (
            direction == "PUT"
            and en1 == "BEARISH"
        )
    ):

        sc["Entry"] = 15

    else:

        blockers.append(
            "1M entry mismatch"
        )

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    recent = c1[-5:]

    if direction == "CALL":

        pb = sum(
            cdir(x)
            == "BEARISH"
            for x in recent
        )

    else:

        pb = sum(
            cdir(x)
            == "BULLISH"
            for x in recent
        )

    if direction == "CALL":

        confirm_dir = "BULLISH"

    else:

        confirm_dir = "BEARISH"

    if (
        cdir(ec)
        == confirm_dir
        and pb >= 2
    ):

        pull = 10

    elif (
        cdir(ec)
        == confirm_dir
        and pb >= 1
    ):

        pull = 6

    else:

        pull = 2

    if pull >= 6:

        sc["Pullback"] = 10

    else:

        blockers.append(
            "No clean pullback"
        )

    # --------------------------------------------------------
    # CONFIRMATION CANDLE
    # --------------------------------------------------------

    if (
        cdir(ec)
        == confirm_dir
        and cs >= MIN_CANDLE
    ):

        sc["Candle"] = 5

    elif cdir(ec) != confirm_dir:

        blockers.append(
            "Confirmation candle mismatch"
        )

    else:

        blockers.append(
            "Confirmation candle weak"
        )

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    recent20 = c5[-21:-1]

    cur = ec["c"]

    ranges = [
        x["h"] - x["l"]
        for x in recent20
        if x["h"] > x["l"]
    ]

    avg = (
        sum(ranges)
        / len(ranges)
        if ranges
        else 0
    )

    if direction == "CALL":

        room = (
            max(
                x["h"]
                for x in recent20
            )
            - cur
        ) / avg if avg else 0

    else:

        room = (
            cur
            - min(
                x["l"]
                for x in recent20
            )
        ) / avg if avg else 0

    roomscore = (
        5
        if room >= 2
        else 4
        if room >= 1.2
        else 3
        if room >= 0.7
        else 2
        if room >= 0.4
        else 1
    )

    if roomscore >= 3:

        sc["Room"] = 5

    else:

        blockers.append(
            "Insufficient room"
        )

    # --------------------------------------------------------
    # EXTENSION
    # --------------------------------------------------------

    e21 = ema(
        a5,
        21,
    )[-1]

    at = atr(
        c5
    )[-1]

    ratio = (
        abs(
            cur
            - e21
        ) / at
        if (
            e21 is not None
            and at
        )
        else 99
    )

    exscore = (
        5
        if ratio <= 0.75
        else 4
        if ratio <= 1.1
        else 3
        if ratio <= 1.5
        else 2
        if ratio <= 2
        else 1
    )

    if exscore >= 3:

        sc["Extension"] = 5

    else:

        blockers.append(
            "Price too extended"
        )

    # --------------------------------------------------------
    # DOMINANCE
    # --------------------------------------------------------

    if dom < MIN_DOM:

        blockers.append(
            "Weak directional dominance"
        )

    # --------------------------------------------------------
    # TOTAL
    # --------------------------------------------------------

    total = (
        sum(sc.values())
        if direction != "NO TRADE"
        else 0
    )

    if (
        direction in (
            "CALL",
            "PUT",
        )
        and total >= MIN_SCORE
        and not blockers
    ):

        final = direction

    else:

        final = "NO TRADE"

    return {
        "symbol": name,
        "provider_symbol": symbol,
        "market_type": market_type,

        "mode": "NORMAL",

        "signal": final,
        "candidate": direction,

        "score": total,
        "dominance": dom,

        "blockers": blockers,

        "trend5": tr5,
        "entry1": en1,
        "structure": st,

        "adx": ax,
        "plus_di": pi,
        "minus_di": mi,

        "rsi": rv,
        "macd_hist": mh,

        "price": cur,

        "entry_time_ms":
            int(
                ec["t"] * 1000
            ),

        "candle_strength":
            cs,

        "pullback_score":
            pull,

        "room_score":
            roomscore,

        "extension_score":
            exscore,

        "breakdown":
            sc,
    }


# ============================================================
# SIGNAL IDS
# ============================================================

def sid(r):

    safe_symbol = (
        r["symbol"]
        .replace(
            "/",
            "",
        )
    )

    return (
        f"{safe_symbol}-"
        f"{r['signal']}-"
        f"{r['entry_time_ms']}"
    )


def signal_key(r):

    return (
        f"{r['mode']}:"
        f"{r['symbol']}:"
        f"{r['signal']}:"
        f"{r['entry_time_ms']}"
    )


# ============================================================
# LOCK
# ============================================================

def locked(r):

    lock_key = (
        r["mode"]
        + ":"
        + r["symbol"]
    )

    until = (
        T["metadata"]
        .get(
            "signal_locks",
            {}
        )
        .get(
            lock_key,
            0,
        )
    )

    return (
        unix()
        < int(until)
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process(r):

    if r["signal"] not in (
        "CALL",
        "PUT",
    ):

        return (
            "rejected",
            None,
        )

    if (
        r["score"]
        < MIN_SCORE
        or r["blockers"]
    ):

        return (
            "rejected",
            None,
        )

    k = signal_key(r)

    id_ = sid(r)

    if (
        k
        in T["metadata"]
        .get(
            "processed_keys",
            [],
        )
    ):

        return (
            "duplicate",
            id_,
        )

    if any(
        x.get("signal_id")
        == id_
        for x in T["signals"]
    ):

        return (
            "duplicate",
            id_,
        )

    if locked(r):

        T["metadata"].setdefault(
            "processed_keys",
            [],
        ).append(k)

        return (
            "locked",
            id_,
        )

    rec = {
        **r,

        "signal_id":
            id_,

        "created_at":
            now(),

        "created_at_unix":
            unix(),

        "result":
            "PENDING",

        "reference_expiry_minutes":
            EXPIRY,

        "data_provider":
            "Twelve Data",
    }

    T["signals"].append(
        rec
    )

    T["metadata"][
        "last_signal"
    ] = now()

    T["metadata"].setdefault(
        "processed_keys",
        []
    ).append(k)

    lock_key = (
        r["mode"]
        + ":"
        + r["symbol"]
    )

    T["metadata"].setdefault(
        "signal_locks",
        {}
    )[lock_key] = (
        unix()
        + LOCK
    )

    T["signals"] = (
        T["signals"]
        [-MAX_ITEMS:]
    )

    T["metadata"][
        "processed_keys"
    ] = (
        T["metadata"]
        ["processed_keys"]
        [-MAX_ITEMS * 2:]
    )

    save_local()

    icon = (
        "💱"
        if r["market_type"]
        == "FX"
        else "🪙"
    )

    direction_text = (
        "CALL / UP"
        if r["signal"]
        == "CALL"
        else
        "PUT / DOWN"
    )

    msg = (
        f"🧠 PRECISION SCANNER "
        f"{VERSION}\n"
        f"━━━━━━━━━━━━━━━━━━\n"

        f"🟢 NEW QUALIFIED SIGNAL\n"

        f"{icon} "
        f"{r['symbol']} "
        f"{direction_text}\n"

        f"📂 Market: "
        f"{r['market_type']}\n"

        f"📡 Data: "
        f"Twelve Data\n"

        f"🎯 Score: "
        f"{r['score']}/100\n"

        f"⏱ Reference expiry: "
        f"{EXPIRY} minutes\n"

        f"💰 Price: "
        f"{price(r['price'])}\n"

        f"📊 5M Trend: "
        f"{r['trend5']}\n"

        f"📈 1M Entry: "
        f"{r['entry1']}\n"

        f"🏗 Structure: "
        f"{r['structure']}\n"

        f"📐 ADX: "
        f"{r['adx']:.1f}\n"

        f"📉 RSI: "
        f"{r['rsi']:.1f}\n"

        f"↗️ +DI: "
        f"{r['plus_di']:.1f}\n"

        f"↘️ -DI: "
        f"{r['minus_di']:.1f}\n"

        f"〽️ MACD Hist: "
        f"{r['macd_hist']:.5f}\n"

        f"🕯 Candle: "
        f"{r['candle_strength']:.2f}\n"

        f"↩️ Pullback: "
        f"{r['pullback_score']}/10\n"

        f"🚪 Room: "
        f"{r['room_score']}/5\n"

        f"📏 Extension: "
        f"{r['extension_score']}/5\n"

        f"🆔 {id_}\n"

        f"🕒 Created: "
        f"{now()}\n\n"

        f"⚠️ Research/test only. "
        f"Score is setup quality, "
        f"not win probability. "
        f"No profit guarantee. "
        f"No trade execution."
    )

    if send(msg):

        status = "alert_sent"

    else:

        T["metadata"].setdefault(
            "delivery_failed_keys",
            []
        ).append(k)

        status = "delivery_failed"

    gh_save()

    return (
        status,
        id_,
    )


# ============================================================
# MARKET LIST
# ============================================================

def build_market_list():

    markets = []

    # Forex first
    for pair in FOREX_PAIRS:

        markets.append(
            {
                "name":
                    pair.replace(
                        "/",
                        "",
                    ),

                "symbol":
                    pair,

                "type":
                    "FX",
            }
        )

    # Crypto second
    for pair in CRYPTO_PAIRS:

        markets.append(
            {
                "name":
                    pair.replace(
                        "/",
                        "",
                    ),

                "symbol":
                    pair,

                "type":
                    "CRYPTO",
            }
        )

    markets = markets[
        :MAX_SCAN_SYMBOLS
    ]

    print(
        "\n[MARKETS] FINAL V5.0 LIST"
    )

    for i, market in enumerate(
        markets,
        1,
    ):

        print(
            f"  {i:02d}. "
            f"{market['type']:6s} "
            f"{market['symbol']}"
        )

    print(
        f"[MARKETS] Total: "
        f"{len(markets)}"
    )

    T["metadata"][
        "markets"
    ] = markets

    return markets


# ============================================================
# REJECTION TRACKING
# ============================================================

def record_rejections(
    r,
    counters,
):

    for reason in r.get(
        "blockers",
        [],
    ):

        counters[reason] = (
            counters.get(
                reason,
                0,
            )
            + 1
        )


# ============================================================
# SCAN
# ============================================================

def scan():

    global MARKETS

    if not TWELVE_KEY:

        print(
            "[FATAL] "
            "TWELVE_DATA_API_KEY "
            "is missing."
        )

        print(
            "[CONFIG] Set your "
            "Twelve Data API key "
            "before scanning."
        )

        return {
            "qualified": 0,
            "borderline": 0,
            "rejected": 0,
            "errors": 1,
            "new": 0,
            "alerts": 0,
            "duplicates": 0,
            "locked": 0,
            "markets": 0,
            "fx": 0,
            "crypto": 0,
        }

    T["metadata"][
        "last_scan"
    ] = now()

    T["metadata"][
        "scan_count"
    ] = int(
        T["metadata"].get(
            "scan_count",
            0,
        )
    ) + 1

    MARKETS = (
        build_market_list()
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
        "fx": 0,
        "crypto": 0,
    }

    rejection_reasons = {}

    for market in MARKETS:

        if market["type"] == "FX":

            summary["fx"] += 1

        else:

            summary["crypto"] += 1

        try:

            result = analyze(
                market
            )

            record_rejections(
                result,
                rejection_reasons,
            )

            # ------------------------------------------------
            # Borderline diagnostics
            # ------------------------------------------------

            if (
                result["signal"]
                == "NO TRADE"
                and
                result["candidate"]
                in (
                    "CALL",
                    "PUT",
                )
                and
                result["score"]
                >= BORDERLINE
            ):

                summary[
                    "borderline"
                ] += 1

                T[
                    "borderline"
                ].append(
                    {
                        **result,
                        "created_at":
                            now(),
                    }
                )

                T[
                    "borderline"
                ] = (
                    T[
                        "borderline"
                    ][-MAX_ITEMS:]
                )

            status, signal_id = (
                process(result)
            )

            if status == "alert_sent":

                summary[
                    "qualified"
                ] += 1

                summary["new"] += 1

                summary["alerts"] += 1

            elif (
                status
                == "delivery_failed"
            ):

                summary[
                    "qualified"
                ] += 1

                summary["new"] += 1

            elif status == "duplicate":

                summary[
                    "duplicates"
                ] += 1

            elif status == "locked":

                summary[
                    "locked"
                ] += 1

            else:

                summary[
                    "rejected"
                ] += 1

        except Exception as e:

            summary["errors"] += 1

            print(
                f"[ERROR] "
                f"{market['symbol']}: "
                f"{e}"
            )

    T["metadata"][
        "last_rejection_reasons"
    ] = rejection_reasons

    save_local()
    gh_save()

    print(
        "\n[REJECTION DIAGNOSTICS]"
    )

    if rejection_reasons:

        ranked = sorted(
            rejection_reasons.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        for reason, count in ranked[:10]:

            print(
                f"  {count:03d} "
                f"{reason}"
            )

    else:

        print(
            "  None recorded."
        )

    print(
        "\n[SUMMARY]",
        json.dumps(
            summary,
            indent=2,
        ),
    )

    return summary


# ============================================================
# STATS
# ============================================================

def stats():

    signals = T[
        "signals"
    ]

    wins = sum(
        x.get("result")
        == "WIN"
        for x in signals
    )

    losses = sum(
        x.get("result")
        == "LOSS"
        for x in signals
    )

    decided = (
        wins
        + losses
    )

    winrate = (
        wins
        / decided
        * 100
        if decided
        else 0
    )

    markets = T[
        "metadata"
    ].get(
        "markets",
        [],
    )

    fx = sum(
        x.get("type")
        == "FX"
        for x in markets
    )

    crypto = sum(
        x.get("type")
        == "CRYPTO"
        for x in markets
    )

    return (
        f"📊 PRECISION SCANNER "
        f"{VERSION}\n\n"

        f"Data provider: "
        f"Twelve Data\n"

        f"Markets: "
        f"{len(markets)}\n"

        f"FX: {fx}\n"
        f"Crypto: {crypto}\n\n"

        f"Recorded: "
        f"{len(signals)}\n"

        f"WIN: {wins}\n"
        f"LOSS: {losses}\n"

        f"Pending: "
        f"{len(signals) - decided}\n\n"

        f"Measured win rate: "
        f"{winrate:.1f}% "
        f"({decided} decided)\n\n"

        f"Historical tracker data only."
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def commands():

    manual = False

    for update in updates():

        update_id = update.get(
            "update_id"
        )

        if isinstance(
            update_id,
            int,
        ):

            T["metadata"][
                "telegram_offset"
            ] = (
                update_id + 1
            )

        message = (
            update.get("message")
            or
            update.get("channel_post")
            or
            {}
        )

        text = str(
            message.get("text")
            or ""
        ).strip()

        if not text:
            continue

        parts = text.split(
            maxsplit=1
        )

        command = (
            parts[0]
            .split("@")[0]
            .lower()
        )

        argument = (
            parts[1].strip()
            if len(parts) > 1
            else ""
        )

        # ----------------------------------------------------
        # HELP
        # ----------------------------------------------------

        if command in (
            "/start",
            "/help",
        ):

            send(
                f"🧠 PRECISION SCANNER "
                f"{VERSION}\n\n"

                f"/scan - run scan now\n"
                f"/stats - tracker statistics\n"
                f"/markets - current markets\n"

                f"/win SIGNAL_ID "
                f"- record WIN\n"

                f"/loss SIGNAL_ID "
                f"- record LOSS\n\n"

                f"Data provider: "
                f"Twelve Data\n"

                f"5M trend + 1M entry\n"
                f"NO TRADE is allowed\n\n"

                f"Research/test framework."
            )

        # ----------------------------------------------------
        # STATS
        # ----------------------------------------------------

        elif command == "/stats":

            send(
                stats()
            )

        # ----------------------------------------------------
        # MARKETS
        # ----------------------------------------------------

        elif command == "/markets":

            markets = T[
                "metadata"
            ].get(
                "markets",
                [],
            )

            if not markets:

                send(
                    "No market list yet."
                )

            else:

                lines = [
                    "📋 V5.0 MARKETS",
                    "━━━━━━━━━━━━━━━━━━",
                ]

                for i, market in enumerate(
                    markets,
                    1,
                ):

                    lines.append(
                        f"{i}. "
                        f"{market.get('type')} "
                        f"{market.get('symbol')}"
                    )

                send(
                    "\n".join(lines)
                )

        # ----------------------------------------------------
        # MANUAL SCAN
        # ----------------------------------------------------

        elif command == "/scan":

            send(
                "🔎 Manual scan requested.\n"
                "Running one scan now."
            )

            manual = True

        # ----------------------------------------------------
        # WIN / LOSS
        # ----------------------------------------------------

        elif command in (
            "/win",
            "/loss",
        ):

            found = next(
                (
                    x
                    for x in T[
                        "signals"
                    ]
                    if x.get(
                        "signal_id"
                    )
                    == argument
                ),
                None,
            )

            if found:

                found["result"] = (
                    "WIN"
                    if command
                    == "/win"
                    else
                    "LOSS"
                )

                found[
                    "result_updated_at"
                ] = now()

                save_local()
                gh_save()

                send(
                    f"✅ Recorded "
                    f"{'WIN' if command == '/win' else 'LOSS'}\n"
                    f"{argument}"
                )

            else:

                send(
                    "❌ Signal not found."
                )

    save_local()

    return manual


# ============================================================
# ONCE
# ============================================================

def once():

    print(
        f"\n=== PRECISION SCANNER "
        f"{VERSION} ONE-SHOT ==="
    )

    if not TWELVE_KEY:

        print(
            "\n[SETUP REQUIRED]"
        )

        print(
            "Set environment variable:"
        )

        print(
            "TWELVE_DATA_API_KEY"
        )

        return

    manual = commands()

    result = scan()

    if manual:

        send(
            f"🔎 MANUAL SCAN COMPLETE\n"

            f"Markets: "
            f"{result['markets']}\n"

            f"FX: "
            f"{result['fx']}\n"

            f"Crypto: "
            f"{result['crypto']}\n"

            f"Qualified: "
            f"{result['qualified']}\n"

            f"Rejected: "
            f"{result['rejected']}\n"

            f"Errors: "
            f"{result['errors']}\n"

            f"New: "
            f"{result['new']}\n"

            f"Alerts: "
            f"{result['alerts']}\n"

            f"Duplicates: "
            f"{result['duplicates']}\n"

            f"Locked: "
            f"{result['locked']}"
        )


# ============================================================
# LOOP
# ============================================================

def loop():

    print(
        f"\n=== PRECISION SCANNER "
        f"{VERSION} "
        f"LOOP / {SCAN_INTERVAL}s ==="
    )

    if not TWELVE_KEY:

        print(
            "\n[FATAL] "
            "TWELVE_DATA_API_KEY "
            "is missing."
        )

        return

    while True:

        try:

            manual = commands()

            result = scan()

            if manual:

                send(
                    f"🔎 MANUAL SCAN COMPLETE\n"

                    f"Markets: "
                    f"{result['markets']}\n"

                    f"FX: "
                    f"{result['fx']}\n"

                    f"Crypto: "
                    f"{result['crypto']}\n"

                    f"Qualified: "
                    f"{result['qualified']}\n"

                    f"Alerts: "
                    f"{result['alerts']}\n"

                    f"Errors: "
                    f"{result['errors']}"
                )

        except KeyboardInterrupt:

            print(
                "\n[STOP] Scanner stopped."
            )

            break

        except Exception as e:

            print(
                "[LOOP ERROR]",
                e,
            )

        print(
            f"\n[WAIT] "
            f"Next scan in "
            f"{SCAN_INTERVAL} seconds..."
        )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "[START]",
        VERSION,
        "Twelve Data market data",
    )

    print(
        "[CONFIG]",
        f"Minimum markets: "
        f"{MIN_SCAN_SYMBOLS}",

        f"Maximum markets: "
        f"{MAX_SCAN_SYMBOLS}",

        f"Scan interval: "
        f"{SCAN_INTERVAL}s",
    )

    if not TWELVE_KEY:

        print(
            "\n[ERROR] "
            "TWELVE_DATA_API_KEY "
            "is not configured."
        )

        print(
            "Create/configure a "
            "Twelve Data API key "
            "and expose it as "
            "TWELVE_DATA_API_KEY."
        )

        sys.exit(1)

    args = [
        x.lower()
        for x in sys.argv[1:]
    ]

    if "--loop" in args:

        loop()

    elif (
        not args
        or "--once" in args
    ):

        once()

    else:

        print(
            "Usage:"
        )

        print(
            "python scanner.py --once"
        )

        print(
            "python scanner.py --loop"
        )

        sys.exit(1)
