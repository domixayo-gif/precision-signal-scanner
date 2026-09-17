#!/usr/bin/env python3
"""
PRECISION SCANNER V4.0
Multi-Market Bybit Scanner
--------------------------------
Bybit public candles -> 5M trend + 1M entry -> filters -> score -> Telegram.

Markets:
- Bybit FX perpetuals discovered automatically
- Bybit crypto linear perpetuals discovered automatically
- Minimum target: 10 instruments
- FX is prioritized, then major/high-volume crypto is added.

Research/test framework only.
No profit guarantee.
No trade execution.
"""

from __future__ import annotations

import base64
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests


# ============================================================
# CONFIGURATION
# ============================================================

VERSION = "V4.0"

ENDPOINTS = [
    "https://api.bybit.com",
    "https://api.bytick.com",
]

# Timeframes
TF5 = "5"
TF1 = "1"

# Reference expiry only
EXPIRY = 5

# Strategy thresholds
MIN_SCORE = 85
BORDERLINE = 80
MIN_DOM = 3
MIN_ADX = 18
MIN_CANDLE = 0.50

CALL_RSI = (43, 68)
PUT_RSI = (32, 57)

LOCK = 300
LIMIT = 220
TIMEOUT = 20
RETRIES = 3

# ------------------------------------------------------------
# MULTI-MARKET SETTINGS
# ------------------------------------------------------------

# The scanner will try to maintain at least this many symbols.
MIN_SCAN_SYMBOLS = 10

# Maximum number of instruments scanned per cycle.
# Increase this if you want more markets.
MAX_SCAN_SYMBOLS = 20

# Maximum crypto instruments used to fill the minimum.
MAX_CRYPTO_SYMBOLS = 17

# These are preference symbols only.
# They are NOT blindly assumed to exist.
PREFERRED_CRYPTO = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "TRXUSDT",
    "SUIUSDT",
    "BCHUSDT",
    "TONUSDT",
    "NEARUSDT",
    "UNIUSDT",
]

# Known Bybit FX symbols.
# These are preference names only.
# Actual discovery comes from Bybit instruments-info.
PREFERRED_FX = [
    "EURUSDUSDT",
    "GBPUSDUSDT",
    "USDJPYUSDT",
]

# Tracker
TRACKER_FILE = "tracker.json"
MAX_ITEMS = 1000

# Telegram
TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# GitHub
GH = os.getenv("GITHUB_TOKEN", "").strip()
REPO = os.getenv("GITHUB_REPOSITORY", "").strip()
ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

# HTTP session
S = requests.Session()
S.headers.update(
    {
        "User-Agent": f"PrecisionScanner/{VERSION}",
        "Accept": "application/json",
    }
)

WORKING = None

# Dynamic market list
MARKETS: list[dict[str, Any]] = []


# ============================================================
# BASIC HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def unix():
    return int(time.time())


def f(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
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


def default():
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
            "working_bybit_endpoint": None,
            "markets": [],
        },
    }


# ============================================================
# TRACKER
# ============================================================

def load():
    try:
        with open(TRACKER_FILE, encoding="utf8") as h:
            d = json.load(h)

        b = default()

        b.update(
            {
                k: d[k]
                for k in ("signals", "borderline")
                if k in d
            }
        )

        b["metadata"].update(d.get("metadata", {}))

        return b

    except Exception:
        return default()


T = load()


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


def gh_save():
    save_local()

    if not ACTIONS or not GH or not REPO:
        return False

    u = f"https://api.github.com/repos/{REPO}/contents/{TRACKER_FILE}"

    hd = {
        "Authorization": f"Bearer {GH}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"PrecisionScanner/{VERSION}",
    }

    try:
        r = S.get(
            u,
            headers=hd,
            timeout=TIMEOUT,
        )

        sha = r.json().get("sha") if r.status_code == 200 else None

        if r.status_code not in (200, 404):
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

        p = {
            "message": f"Update tracker {VERSION}",
            "content": base64.b64encode(raw).decode(),
        }

        if sha:
            p["sha"] = sha

        r = S.put(
            u,
            headers=hd,
            json=p,
            timeout=TIMEOUT,
        )

        print(
            "[TRACKER] GitHub save",
            r.status_code,
        )

        return r.status_code in (200, 201)

    except Exception as e:
        print(
            "[TRACKER] save error",
            e,
        )
        return False


# ============================================================
# TELEGRAM
# ============================================================

def tg(method, **kw):
    if not TG:
        return None

    try:
        return S.post(
            f"https://api.telegram.org/bot{TG}/{method}",
            timeout=TIMEOUT + 5,
            **kw,
        )

    except Exception as e:
        print("[TELEGRAM]", e)
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
            "text": text[:3900],
            "disable_web_page_preview": True,
        },
    )

    ok = bool(
        r and r.status_code == 200
    )

    if not ok:
        print(
            "[TELEGRAM] send failed",
            r.status_code if r else "exception",
            r.text[:300] if r else "",
        )

    return ok


def updates():
    if not TG:
        return []

    p = {
        "timeout": 1,
        "limit": 20,
    }

    off = T["metadata"].get(
        "telegram_offset"
    )

    if off is not None:
        p["offset"] = off

    r = tg(
        "getUpdates",
        params=p,
    )

    if not r or r.status_code != 200:
        return []

    try:
        return r.json().get(
            "result",
            [],
        )

    except Exception:
        return []


# ============================================================
# INDICATORS
# ============================================================

def ema(a, n):
    if len(a) < n:
        return [None] * len(a)

    z = [None] * len(a)

    prev = sum(a[:n]) / n
    z[n - 1] = prev

    k = 2 / (n + 1)

    for i in range(n, len(a)):
        prev = (
            (a[i] - prev) * k
            + prev
        )

        z[i] = prev

    return z


def rsi(a, n=14):
    z = [None] * len(a)

    if len(a) <= n:
        return z

    g = (
        sum(
            max(a[i] - a[i - 1], 0)
            for i in range(1, n + 1)
        )
        / n
    )

    l = (
        sum(
            max(a[i - 1] - a[i], 0)
            for i in range(1, n + 1)
        )
        / n
    )

    def q(g, l):
        return (
            100
            if l == 0
            else 100 - 100 / (1 + g / l)
        )

    z[n] = q(g, l)

    for i in range(n + 1, len(a)):
        d = a[i] - a[i - 1]

        g = (
            (g * (n - 1))
            + max(d, 0)
        ) / n

        l = (
            (l * (n - 1))
            + max(-d, 0)
        ) / n

        z[i] = q(g, l)

    return z


def atr(c, n=14):
    if len(c) <= n:
        return [None] * len(c)

    tr = [0]

    for i in range(1, len(c)):
        tr.append(
            max(
                c[i]["h"] - c[i]["l"],
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

    p = sum(tr[1:n + 1]) / n
    z[n] = p

    for i in range(n + 1, len(c)):
        p = (
            (p * (n - 1))
            + tr[i]
        ) / n

        z[i] = p

    return z


def macd(a):
    e12 = ema(a, 12)
    e26 = ema(a, 26)

    m = [None] * len(a)
    idx = []
    vals = []

    for i in range(len(a)):
        if (
            e12[i] is not None
            and e26[i] is not None
        ):
            m[i] = e12[i] - e26[i]
            idx.append(i)
            vals.append(m[i])

    se = ema(vals, 9)

    sig = [None] * len(a)
    hist = [None] * len(a)

    for j, i in enumerate(idx):
        sig[i] = se[j]

        if sig[i] is not None:
            hist[i] = m[i] - sig[i]

    return hist


def adx(c, n=14):
    L = len(c)

    tr = [0] * L
    pd = [0] * L
    md = [0] * L

    for i in range(1, L):
        tr[i] = max(
            c[i]["h"] - c[i]["l"],
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
            if u > d and u > 0
            else 0
        )

        md[i] = (
            d
            if d > u and d > 0
            else 0
        )

    z = [None] * L
    plus = [None] * L
    minus = [None] * L
    dx = [None] * L

    if L <= 2 * n:
        return z, plus, minus

    at = sum(tr[1:n + 1]) / n
    pp = sum(pd[1:n + 1]) / n
    mm = sum(md[1:n + 1]) / n

    for i in range(n, L):

        if i > n:
            at = (
                (at * (n - 1))
                + tr[i]
            ) / n

            pp = (
                (pp * (n - 1))
                + pd[i]
            ) / n

            mm = (
                (mm * (n - 1))
                + md[i]
            ) / n

        if at:
            plus[i] = 100 * pp / at
            minus[i] = 100 * mm / at

            den = plus[i] + minus[i]

            if den:
                dx[i] = (
                    100
                    * abs(
                        plus[i]
                        - minus[i]
                    )
                    / den
                )

    valid = [
        x for x in dx
        if x is not None
    ]

    if len(valid) < n:
        return z, plus, minus

    count = 0
    start = None
    seed = 0

    for i in range(n, L):

        if dx[i] is not None:
            seed += dx[i]
            count += 1

            if count == n:
                start = i
                break

    if start is None:
        return z, plus, minus

    p = seed / n
    z[start] = p

    for i in range(start + 1, L):

        if dx[i] is not None:
            p = (
                (p * (n - 1))
                + dx[i]
            ) / n

            z[i] = p

    return z, plus, minus


# ============================================================
# CANDLE DATA
# ============================================================

def candles(rows):
    out = []

    for r in rows:

        if len(r) < 5:
            continue

        ts, o, h, l, c = map(
            f,
            r[:5],
        )

        if (
            None in (
                ts,
                o,
                h,
                l,
                c,
            )
            or h < l
        ):
            continue

        out.append(
            {
                "t": ts,
                "o": o,
                "h": h,
                "l": l,
                "c": c,
            }
        )

    return list(reversed(out))


# ============================================================
# BYBIT API
# ============================================================

def api_get(path, params):
    global WORKING

    eps = (
        [WORKING]
        if WORKING
        else []
    ) + [
        e for e in ENDPOINTS
        if e != WORKING
    ]

    last = "unknown"

    for ep in eps:

        for attempt in range(
            1,
            RETRIES + 1,
        ):

            try:

                r = S.get(
                    ep + path,
                    params=params,
                    timeout=TIMEOUT,
                )

                if r.status_code != 200:

                    last = (
                        f"HTTP {r.status_code}: "
                        f"{r.text[:250]}"
                    )

                    print(
                        "[BYBIT]",
                        last,
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

                p = r.json()

                if p.get("retCode") != 0:

                    last = (
                        f"retCode="
                        f"{p.get('retCode')} "
                        f"{p.get('retMsg')}"
                    )

                    print(
                        "[BYBIT]",
                        last,
                    )

                    break

                WORKING = ep

                T["metadata"][
                    "working_bybit_endpoint"
                ] = ep

                return p

            except Exception as e:

                last = str(e)

                print(
                    "[BYBIT] exception",
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
        f"Bybit API failed: {last}"
    )


# ============================================================
# MARKET DISCOVERY
# ============================================================

def discover_instruments():
    """
    Discover active Bybit linear instruments.

    FX contracts are identified from the FX symbols
    actually returned by Bybit.

    Crypto contracts are identified as USDT linear
    contracts and ranked by 24h turnover.
    """

    print(
        "[MARKETS] Discovering Bybit instruments..."
    )

    instruments = []

    cursor = None

    while True:

        params = {
            "category": "linear",
            "limit": 1000,
        }

        if cursor:
            params["cursor"] = cursor

        try:
            p = api_get(
                "/v5/market/instruments-info",
                params,
            )

        except Exception as e:
            print(
                "[MARKETS] instrument discovery failed:",
                e,
            )
            break

        result = p.get(
            "result",
            {},
        )

        page = result.get(
            "list",
            [],
        )

        instruments.extend(page)

        cursor = result.get(
            "nextPageCursor"
        )

        if not cursor or not page:
            break

    print(
        f"[MARKETS] Discovered {len(instruments)} linear instruments"
    )

    return instruments


def get_tickers():
    """
    Get 24h ticker information for ranking
    liquid crypto markets.
    """

    try:

        p = api_get(
            "/v5/market/tickers",
            {
                "category": "linear",
            },
        )

        return p.get(
            "result",
            {}).get(
            "list",
            [],
        )

    except Exception as e:

        print(
            "[MARKETS] ticker discovery failed:",
            e,
        )

        return []


def is_active_linear(x):
    if not isinstance(x, dict):
        return False

    if x.get("status") not in (
        None,
        "",
        "Trading",
    ):
        return False

    symbol = str(
        x.get("symbol", "")
    ).upper()

    if not symbol:
        return False

    if not symbol.endswith("USDT"):
        return False

    return True


def discover_fx(instruments):
    """
    Return FX symbols that Bybit actually exposes.

    The preferred list is used first, but we only accept
    symbols confirmed by the live instruments endpoint.
    """

    available = {
        str(x.get("symbol", "")).upper(): x
        for x in instruments
        if is_active_linear(x)
    }

    found = []

    # Preferred known FX contracts
    for sym in PREFERRED_FX:

        sym = sym.upper()

        if sym in available:

            found.append(
                {
                    "symbol": sym,
                    "type": "FX",
                    "base": sym.replace(
                        "USDT",
                        "",
                    ),
                }
            )

    # Additional FX-looking symbols.
    #
    # This intentionally uses a conservative allow-list of
    # currency bases so random crypto symbols are not
    # classified as FX.
    fx_bases = {
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "AUDUSD",
        "NZDUSD",
        "USDCAD",
        "USDCHF",
        "EURGBP",
        "EURJPY",
        "GBPJPY",
        "AUDJPY",
        "EURCHF",
        "EURAUD",
        "GBPAUD",
        "GBPCAD",
        "CADJPY",
        "CHFJPY",
        "AUDCAD",
        "AUDCHF",
        "NZDJPY",
        "NZDCHF",
        "NZDCAD",
        "EURNZD",
        "GBPNZD",
    }

    for sym, info in available.items():

        if not sym.endswith("USDT"):
            continue

        base = sym[:-4]

        if base in fx_bases:

            if not any(
                x["symbol"] == sym
                for x in found
            ):
                found.append(
                    {
                        "symbol": sym,
                        "type": "FX",
                        "base": base,
                    }
                )

    return found


def discover_crypto(
    instruments,
    tickers,
    exclude_symbols=None,
):
    """
    Select liquid USDT crypto perpetuals.

    Preference coins are selected first when available.
    Remaining slots are filled by 24h turnover.
    """

    if exclude_symbols is None:
        exclude_symbols = set()

    available = {
        str(x.get("symbol", "")).upper(): x
        for x in instruments
        if is_active_linear(x)
    }

    # Symbols that are definitely not desired as normal
    # crypto candidates.
    excluded = set(exclude_symbols)

    selected = []

    # --------------------------------------------------------
    # 1. Preferred major crypto
    # --------------------------------------------------------

    for sym in PREFERRED_CRYPTO:

        sym = sym.upper()

        if sym in excluded:
            continue

        if sym not in available:
            continue

        selected.append(
            {
                "symbol": sym,
                "type": "CRYPTO",
                "base": sym.replace(
                    "USDT",
                    "",
                ),
            }
        )

        if len(selected) >= MAX_CRYPTO_SYMBOLS:
            return selected

    # --------------------------------------------------------
    # 2. Fill using turnover
    # --------------------------------------------------------

    turnover = {}

    for x in tickers:

        sym = str(
            x.get("symbol", "")
        ).upper()

        if not sym:
            continue

        turnover[sym] = (
            f(x.get("turnover24h"))
            or 0
        )

    candidates = []

    for sym in available:

        if sym in excluded:
            continue

        if sym in {
            x["symbol"]
            for x in selected
        }:
            continue

        if not sym.endswith("USDT"):
            continue

        # Avoid obvious FX bases
        base = sym[:-4]

        if base in {
            "EURUSD",
            "GBPUSD",
            "USDJPY",
            "AUDUSD",
            "NZDUSD",
            "USDCAD",
            "USDCHF",
            "EURGBP",
            "EURJPY",
            "GBPJPY",
            "AUDJPY",
            "EURCHF",
            "EURAUD",
            "GBPAUD",
            "GBPCAD",
            "CADJPY",
            "CHFJPY",
            "AUDCAD",
            "AUDCHF",
            "NZDJPY",
            "NZDCHF",
            "NZDCAD",
            "EURNZD",
            "GBPNZD",
        }:
            continue

        candidates.append(
            (
                turnover.get(sym, 0),
                sym,
            )
        )

    candidates.sort(
        reverse=True
    )

    for _, sym in candidates:

        selected.append(
            {
                "symbol": sym,
                "type": "CRYPTO",
                "base": sym[:-4],
            }
        )

        if len(selected) >= MAX_CRYPTO_SYMBOLS:
            break

    return selected


def build_market_list():
    """
    Build final market list.

    Priority:
        1. All discovered FX contracts
        2. Preferred major crypto
        3. Highest-turnover crypto

    Goal:
        at least 10 total whenever Bybit has enough
        eligible instruments.
    """

    instruments = discover_instruments()

    tickers = get_tickers()

    fx = discover_fx(
        instruments
    )

    fx_symbols = {
        x["symbol"]
        for x in fx
    }

    needed_crypto = max(
        0,
        MIN_SCAN_SYMBOLS
        - len(fx),
    )

    global MAX_CRYPTO_SYMBOLS

    old_max_crypto = MAX_CRYPTO_SYMBOLS

    MAX_CRYPTO_SYMBOLS = max(
        MAX_CRYPTO_SYMBOLS,
        needed_crypto,
    )

    crypto = discover_crypto(
        instruments,
        tickers,
        exclude_symbols=fx_symbols,
    )

    MAX_CRYPTO_SYMBOLS = old_max_crypto

    # Limit crypto if necessary
    crypto = crypto[
        :max(
            needed_crypto,
            min(
                len(crypto),
                MAX_CRYPTO_SYMBOLS,
            ),
        )
    ]

    combined = fx + crypto

    # Final hard cap
    combined = combined[
        :MAX_SCAN_SYMBOLS
    ]

    if len(combined) < MIN_SCAN_SYMBOLS:

        print(
            f"[MARKETS] WARNING: only "
            f"{len(combined)} eligible markets found. "
            f"Target is {MIN_SCAN_SYMBOLS}."
        )

    print(
        "\n[MARKETS] FINAL SCAN LIST"
    )

    for i, m in enumerate(
        combined,
        1,
    ):
        print(
            f"  {i:02d}. "
            f"{m['type']:6s} "
            f"{m['symbol']}"
        )

    print(
        f"[MARKETS] Total: {len(combined)}"
    )

    T["metadata"][
        "markets"
    ] = combined

    return combined


# ============================================================
# CANDLE FETCHING
# ============================================================

def fetch(sym, interval):

    try:

        p = api_get(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": sym,
                "interval": interval,
                "limit": LIMIT,
            },
        )

        rows = (
            p.get(
                "result",
                {}
            ).get(
                "list",
                []
            )
        )

        if not rows:
            raise RuntimeError(
                "empty kline"
            )

        print(
            f"[BYBIT] OK {sym} "
            f"{interval}: "
            f"{len(rows)} candles"
        )

        return rows

    except Exception as e:

        raise RuntimeError(
            f"Unable to fetch "
            f"{sym} {interval}: {e}"
        )


# ============================================================
# STRATEGY
# ============================================================

def trend(c):

    a = [
        x["c"]
        for x in c
    ]

    e9 = ema(a, 9)[-1]
    e21 = ema(a, 21)[-1]
    e50 = ema(a, 50)[-1]

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


def structure(c, n=8):

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

    if h2 > h1 and l2 > l1:
        return "BULLISH"

    if h2 < h1 and l2 < l1:
        return "BEARISH"

    return (
        "BULLISH"
        if a[-1]["c"] > a[0]["c"]
        else "BEARISH"
        if a[-1]["c"] < a[0]["c"]
        else "NEUTRAL"
    )


def strength(x):

    r = x["h"] - x["l"]

    return (
        abs(
            x["c"]
            - x["o"]
        ) / r
        if r > 0
        else 0
    )


def cdir(x):

    if x["c"] > x["o"]:
        return "BULLISH"

    if x["c"] < x["o"]:
        return "BEARISH"

    return "NEUTRAL"


def analyze(
    name,
    sym,
    market_type="CRYPTO",
):

    print(
        f"[SCAN] {name} "
        f"({sym}) "
        f"type={market_type}"
    )

    c5 = candles(
        fetch(
            sym,
            TF5,
        )
    )

    c1 = candles(
        fetch(
            sym,
            TF1,
        )
    )

    if len(c5) < 60 or len(c1) < 60:
        raise RuntimeError(
            f"{name}: insufficient candles"
        )

    a5 = [
        x["c"]
        for x in c5
    ]

    tr5 = trend(c5)
    en1 = trend(c1)
    st = structure(c5)

    rv = rsi(a5)[-1]
    mh = macd(a5)[-1]

    av, pdi, mdi = adx(c5)

    ax = av[-1]
    pi = pdi[-1]
    mi = mdi[-1]

    ec = c1[-1]
    cs = strength(ec)

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

    if votes["CALL"] == votes["PUT"]:

        direction = "NO TRADE"
        dom = 0

    else:

        direction = (
            "CALL"
            if votes["CALL"] > votes["PUT"]
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

    # Trend
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

    # Structure
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

    # ADX / DMI
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

    # MACD
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

    # RSI
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

    # 1M entry
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

    # Pullback
    recent = c1[-5:]

    pb = (
        sum(
            cdir(x) == "BEARISH"
            for x in recent
        )
        if direction == "CALL"
        else
        sum(
            cdir(x) == "BULLISH"
            for x in recent
        )
    )

    pull = (
        10
        if (
            cdir(ec)
            == (
                "BULLISH"
                if direction == "CALL"
                else "BEARISH"
            )
            and pb >= 2
        )
        else
        6
        if (
            cdir(ec)
            == (
                "BULLISH"
                if direction == "CALL"
                else "BEARISH"
            )
            and pb >= 1
        )
        else 2
    )

    if pull >= 6:

        sc["Pullback"] = 10

    else:

        blockers.append(
            "No clean pullback"
        )

    # Confirmation candle
    if (
        cdir(ec)
        == (
            "BULLISH"
            if direction == "CALL"
            else "BEARISH"
        )
        and cs >= MIN_CANDLE
    ):

        sc["Candle"] = 5

    elif (
        cdir(ec)
        != (
            "BULLISH"
            if direction == "CALL"
            else "BEARISH"
        )
    ):

        blockers.append(
            "Confirmation candle mismatch"
        )

    else:

        blockers.append(
            "Confirmation candle weak"
        )

    # Room
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

    # Extension
    e21 = ema(
        a5,
        21,
    )[-1]

    at = atr(
        c5
    )[-1]

    ratio = (
        abs(
            cur - e21
        ) / at
        if e21 is not None
        and at
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

    # Dominance
    if dom < MIN_DOM:

        blockers.append(
            "Weak directional dominance"
        )

    total = (
        sum(sc.values())
        if direction != "NO TRADE"
        else 0
    )

    final = (
        direction
        if (
            direction in (
                "CALL",
                "PUT",
            )
            and total >= MIN_SCORE
            and not blockers
        )
        else "NO TRADE"
    )

    return {
        "symbol": name,
        "bybit_symbol": sym,
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
        "entry_time_ms": int(
            ec["t"]
        ),
        "candle_strength": cs,
        "pullback_score": pull,
        "room_score": roomscore,
        "extension_score": exscore,
        "breakdown": sc,
    }


# ============================================================
# SIGNAL PROCESSING
# ============================================================

def sid(r):

    return (
        f"{r['symbol']}-"
        f"{r['signal']}-"
        f"{r['entry_time_ms']}"
    )


def key(r):

    return (
        f"{r['mode']}:"
        f"{r['symbol']}:"
        f"{r['signal']}:"
        f"{r['entry_time_ms']}"
    )


def locked(r):

    return (
        unix()
        < int(
            T["metadata"]
            .get(
                "signal_locks",
                {}
            )
            .get(
                r["mode"]
                + ":"
                + r["symbol"],
                0,
            )
        )
    )


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
        r["score"] < MIN_SCORE
        or r["blockers"]
    ):
        return (
            "rejected",
            None,
        )

    k = key(r)
    id_ = sid(r)

    if (
        k
        in T["metadata"].get(
            "processed_keys",
            [],
        )
        or any(
            x.get("signal_id")
            == id_
            for x in T["signals"]
        )
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
        "signal_id": id_,
        "created_at": now(),
        "created_at_unix": unix(),
        "result": "PENDING",
        "reference_expiry_minutes": EXPIRY,
    }

    T["signals"].append(rec)

    T["metadata"][
        "last_signal"
    ] = now()

    T["metadata"].setdefault(
        "processed_keys",
        []
    ).append(k)

    T["metadata"].setdefault(
        "signal_locks",
        {}
    )[
        r["mode"]
        + ":"
        + r["symbol"]
    ] = unix() + LOCK

    T["signals"] = T[
        "signals"
    ][-MAX_ITEMS:]

    T["metadata"][
        "processed_keys"
    ] = T["metadata"][
        "processed_keys"
    ][-MAX_ITEMS * 2:]

    save_local()

    market_icon = (
        "💱"
        if r.get("market_type")
        == "FX"
        else "🪙"
    )

    msg = (
        f"🧠 PRECISION SCANNER {VERSION}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🟢 NEW QUALIFIED SIGNAL\n"
        f"{market_icon} "
        f"{r['symbol']} "
        f"{'CALL / UP' if r['signal'] == 'CALL' else 'PUT / DOWN'}\n"
        f"📂 Market: {r.get('market_type', 'CRYPTO')}\n"
        f"🎯 Score: {r['score']}/100\n"
        f"⏱ Reference expiry: {EXPIRY} minutes\n"
        f"💰 Price: {price(r['price'])}\n"
        f"📊 5M Trend: {r['trend5']}\n"
        f"📈 1M Entry: {r['entry1']}\n"
        f"🏗 Structure: {r['structure']}\n"
        f"📐 ADX: {r['adx']:.1f}\n"
        f"📉 RSI: {r['rsi']:.1f}\n"
        f"↗️ +DI: {r['plus_di']:.1f}\n"
        f"↘️ -DI: {r['minus_di']:.1f}\n"
        f"〽️ MACD Hist: {r['macd_hist']:.5f}\n"
        f"🕯 Candle: {r['candle_strength']:.2f}\n"
        f"↩️ Pullback: {r['pullback_score']}/10\n"
        f"🚪 Room: {r['room_score']}/5\n"
        f"📏 Extension: {r['extension_score']}/5\n"
        f"🆔 {id_}\n"
        f"🕒 Created: {now()}\n\n"
        f"⚠️ Test/research only. "
        f"Score is setup quality, "
        f"not win probability. "
        f"No profit guarantee. "
        f"Scanner does not place trades."
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
# SCANNER
# ============================================================

def scan():

    global MARKETS

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

    # Refresh market list every scan
    MARKETS = build_market_list()

    s = {
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

    for market in MARKETS:

        name = market["base"]
        sym = market["symbol"]
        market_type = market["type"]

        if market_type == "FX":
            s["fx"] += 1
        else:
            s["crypto"] += 1

        try:

            r = analyze(
                name,
                sym,
                market_type,
            )

            st, id_ = process(r)

            if st == "alert_sent":

                s["qualified"] += 1
                s["new"] += 1
                s["alerts"] += 1

            elif st == "delivery_failed":

                s["qualified"] += 1
                s["new"] += 1

            elif st == "duplicate":

                s["duplicates"] += 1

            elif st == "locked":

                s["locked"] += 1

            else:

                s["rejected"] += 1

        except Exception as e:

            s["errors"] += 1

            print(
                f"[ERROR] "
                f"{name} "
                f"({sym}): "
                f"{e}"
            )

    save_local()
    gh_save()

    print(
        "[SUMMARY]",
        json.dumps(
            s,
            indent=2,
        ),
    )

    return s


# ============================================================
# STATS
# ============================================================

def stats():

    a = T["signals"]

    w = sum(
        x.get("result") == "WIN"
        for x in a
    )

    l = sum(
        x.get("result") == "LOSS"
        for x in a
    )

    d = w + l

    wr = (
        w / d * 100
        if d
        else 0
    )

    markets = T[
        "metadata"
    ].get(
        "markets",
        [],
    )

    fx = sum(
        x.get("type") == "FX"
        for x in markets
    )

    crypto = sum(
        x.get("type") == "CRYPTO"
        for x in markets
    )

    return (
        f"📊 PRECISION SCANNER {VERSION}\n\n"
        f"Markets currently scanned: "
        f"{len(markets)}\n"
        f"FX: {fx}\n"
        f"Crypto: {crypto}\n\n"
        f"Recorded: {len(a)}\n"
        f"WIN: {w}\n"
        f"LOSS: {l}\n"
        f"Pending: {len(a) - d}\n"
        f"Measured win rate: "
        f"{wr:.1f}% ({d} decided)\n\n"
        f"Historical tracker data only."
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def commands():

    manual = False

    for u in updates():

        uid = u.get(
            "update_id"
        )

        if isinstance(uid, int):
            T["metadata"][
                "telegram_offset"
            ] = uid + 1

        m = (
            u.get("message")
            or u.get("channel_post")
            or {}
        )

        text = str(
            m.get("text")
            or ""
        ).strip()

        if not text:
            continue

        p = text.split(
            maxsplit=1
        )

        cmd = (
            p[0]
            .split("@")[0]
            .lower()
        )

        arg = (
            p[1].strip()
            if len(p) > 1
            else ""
        )

        if cmd in (
            "/start",
            "/help",
        ):

            send(
                f"🧠 PRECISION SCANNER {VERSION}\n\n"
                f"/scan - run a scan now\n"
                f"/stats - tracker statistics\n"
                f"/markets - show current markets\n"
                f"/win SIGNAL_ID - record WIN\n"
                f"/loss SIGNAL_ID - record LOSS\n\n"
                f"Multi-market Bybit scanner.\n"
                f"5M trend + 1M entry.\n"
                f"NO TRADE is allowed.\n\n"
                f"Test framework only."
            )

        elif cmd == "/stats":

            send(stats())

        elif cmd == "/markets":

            markets = T[
                "metadata"
            ].get(
                "markets",
                [],
            )

            if not markets:

                send(
                    "No market list yet. "
                    "Run /scan first."
                )

            else:

                lines = [
                    "📋 CURRENT MARKETS",
                    "━━━━━━━━━━━━━━━━━━",
                ]

                for i, x in enumerate(
                    markets,
                    1,
                ):

                    lines.append(
                        f"{i}. "
                        f"{x.get('type')} "
                        f"{x.get('symbol')}"
                    )

                send(
                    "\n".join(lines)
                )

        elif cmd == "/scan":

            send(
                "🔎 Manual scan requested. "
                "Running one scan now."
            )

            manual = True

        elif cmd in (
            "/win",
            "/loss",
        ):

            found = next(
                (
                    x
                    for x in T["signals"]
                    if x.get(
                        "signal_id"
                    )
                    == arg
                ),
                None,
            )

            if found:

                found["result"] = (
                    "WIN"
                    if cmd == "/win"
                    else "LOSS"
                )

                found[
                    "result_updated_at"
                ] = now()

                save_local()
                gh_save()

                send(
                    f"✅ Recorded "
                    f"{'WIN' if cmd == '/win' else 'LOSS'}\n"
                    f"{arg}"
                )

            else:

                send(
                    "❌ Signal not found."
                )

    save_local()

    return manual


# ============================================================
# RUN MODES
# ============================================================

def once():

    print(
        f"=== PRECISION SCANNER "
        f"{VERSION} ONE-SHOT ==="
    )

    manual = commands()

    s = scan()

    if manual:

        send(
            f"🔎 MANUAL SCAN COMPLETE\n"
            f"Markets: {s['markets']}\n"
            f"FX: {s['fx']}\n"
            f"Crypto: {s['crypto']}\n"
            f"Qualified: {s['qualified']}\n"
            f"Rejected: {s['rejected']}\n"
            f"Errors: {s['errors']}\n"
            f"New: {s['new']}\n"
            f"Alerts: {s['alerts']}\n"
            f"Duplicates: {s['duplicates']}\n"
            f"Locked: {s['locked']}"
        )


def loop():

    print(
        f"=== PRECISION SCANNER "
        f"{VERSION} LOOP / 300s ==="
    )

    while True:

        try:

            manual = commands()

            s = scan()

        except KeyboardInterrupt:

            break

        except Exception as e:

            print(
                "[LOOP ERROR]",
                e,
            )

            manual = False
            s = None

        if manual and s:

            send(
                f"🔎 MANUAL SCAN COMPLETE\n"
                f"Markets: {s['markets']}\n"
                f"FX: {s['fx']}\n"
                f"Crypto: {s['crypto']}\n"
                f"Qualified: {s['qualified']}\n"
                f"Alerts: {s['alerts']}\n"
                f"Errors: {s['errors']}"
            )

        time.sleep(300)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "[START]",
        VERSION,
        "Bybit public market data",
    )

    print(
        "[CONFIG]",
        f"Minimum markets: {MIN_SCAN_SYMBOLS}",
        f"Maximum markets: {MAX_SCAN_SYMBOLS}",
    )

    if (
        sys.argv[1:]
        and sys.argv[1].lower()
        == "--loop"
    ):

        loop()

    elif (
        not sys.argv[1:]
        or sys.argv[1].lower()
        == "--once"
    ):

        once()

    else:

        print(
            "Usage: "
            "python scanner.py "
            "--once | --loop"
        )

        sys.exit(1)
