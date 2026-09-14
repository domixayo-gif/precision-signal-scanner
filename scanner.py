import os
import html
import json
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

BASE_URL = "https://api.exchange.coinbase.com"
TIMEOUT = 20

TRACKER_FILE = "tracker.json"

# Pocket Option OTC assets
ASSETS = {
    "BTC": "Bitcoin OTC",
    "ETH": "Ethereum OTC",
    "SOL": "Solana OTC",
    "BNB": "BNB OTC",
    "ADA": "Cardano OTC",
    "TRX": "TRON OTC",
    "LINK": "Chainlink OTC",
    "TON": "Toncoin OTC",
    "AVAX": "Avalanche OTC",
    "DOGE": "Dogecoin OTC",
    "DOT": "Polkadot OTC",
    "LTC": "Litecoin OTC",
    "POL": "Polygon OTC",
}


# ============================================================
# HTTP
# ============================================================

def request_json(url, params=None):
    response = requests.get(
        url,
        params=params,
        headers={
            "Accept": "application/json",
            "User-Agent": "precision-signal-scanner-v2",
        },
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    return response.json()


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )

    response.raise_for_status()


# ============================================================
# TRACKER
# ============================================================

def default_tracker():
    return {
        "telegram_update_offset": 0,

        "legacy_baseline": {
            "signals": 100,
            "wins": 58,
            "losses": 42,
            "call_wins": 31,
            "call_losses": 19,
            "put_wins": 27,
            "put_losses": 23,
            "score_rates": {
                "8": 48,
                "9": 55,
                "10": 63,
                "11": 68,
            },
            "best_asset": "LINK OTC",
            "worst_asset": "ETH OTC",
        },

        "signals": []
    }


def load_tracker():
    if not os.path.exists(TRACKER_FILE):
        return default_tracker()

    try:
        with open(
            TRACKER_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if "signals" not in data:
            data["signals"] = []

        if "telegram_update_offset" not in data:
            data["telegram_update_offset"] = 0

        if "legacy_baseline" not in data:
            data["legacy_baseline"] = default_tracker()[
                "legacy_baseline"
            ]

        return data

    except Exception:
        return default_tracker()


def save_tracker(tracker):
    with open(
        TRACKER_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            tracker,
            file,
            indent=2,
        )


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None

    value = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for price in values[period:]:
        value = (
            price * multiplier
            + value * (1 - multiplier)
        )

    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        average_gain = (
            average_gain * (period - 1)
            + gains[i]
        ) / period

        average_loss = (
            average_loss * (period - 1)
            + losses[i]
        ) / period

    if average_loss == 0:
        return 100.0

    rs = average_gain / average_loss

    return 100 - (100 / (1 + rs))


def macd(values):
    if len(values) < 60:
        raise RuntimeError("Not enough candles for MACD")

    macd_values = []

    for i in range(26, len(values) + 1):
        fast = ema(values[:i], 12)
        slow = ema(values[:i], 26)

        macd_values.append(
            fast - slow
        )

    line = macd_values[-1]
    signal = ema(macd_values, 9)

    if signal is None:
        raise RuntimeError("Not enough MACD data")

    histogram = line - signal

    return line, signal, histogram


# ============================================================
# MARKET DATA
# ============================================================

def get_products():
    data = request_json(
        f"{BASE_URL}/products"
    )

    products = {}

    for product in data:
        if product.get("status") != "online":
            continue

        base = product.get("base_currency")
        quote = product.get("quote_currency")

        if (
            base in ASSETS
            and quote in ("USD", "USDC")
        ):
            if base not in products:
                products[base] = product["id"]

    return products


def get_closes(product_id, granularity):
    data = request_json(
        f"{BASE_URL}/products/{product_id}/candles",
        {
            "granularity": granularity
        },
    )

    if not isinstance(data, list):
        raise RuntimeError(
            "Invalid candle response"
        )

    if len(data) < 60:
        raise RuntimeError(
            f"Only {len(data)} candles received"
        )

    data.sort(
        key=lambda candle: candle[0]
    )

    closes = [
        float(candle[4])
        for candle in data
    ]

    # Remove newest candle because it may
    # still be forming.
    if len(closes) > 60:
        closes = closes[:-1]

    return closes[-200:]


# ============================================================
# V2 ANALYSIS
# ============================================================

def analyze(symbol, product_id):
    five = get_closes(
        product_id,
        300,
    )

    one = get_closes(
        product_id,
        60,
    )

    if len(five) < 60:
        raise RuntimeError(
            "Not enough 5M candles"
        )

    if len(one) < 60:
        raise RuntimeError(
            "Not enough 1M candles"
        )

    # -------------------------------
    # 5 MINUTE TREND
    # -------------------------------

    price5 = five[-1]

    ema20_5 = ema(
        five,
        20,
    )

    ema50_5 = ema(
        five,
        50,
    )

    ema20_5_previous = ema(
        five[:-3],
        20,
    )

    rsi5 = rsi(five)

    macd5_line, macd5_signal, macd5_hist = macd(
        five
    )

    # -------------------------------
    # 1 MINUTE ENTRY
    # -------------------------------

    price1 = one[-1]
    previous1 = one[-2]

    ema9_1 = ema(
        one,
        9,
    )

    ema21_1 = ema(
        one,
        21,
    )

    rsi1 = rsi(one)

    macd1_line, macd1_signal, macd1_hist = macd(
        one
    )

    bull = 0
    bear = 0

    reasons_bull = []
    reasons_bear = []

    # ========================================================
    # 5M CONDITIONS
    # ========================================================

    # 1. Price vs EMA20
    if price5 > ema20_5:
        bull += 2
        reasons_bull.append(
            "5M price above EMA20"
        )
    elif price5 < ema20_5:
        bear += 2
        reasons_bear.append(
            "5M price below EMA20"
        )

    # 2. EMA20 vs EMA50
    if ema20_5 > ema50_5:
        bull += 2
        reasons_bull.append(
            "5M EMA20 above EMA50"
        )
    elif ema20_5 < ema50_5:
        bear += 2
        reasons_bear.append(
            "5M EMA20 below EMA50"
        )

    # 3. EMA20 slope
    if ema20_5 > ema20_5_previous:
        bull += 1
        reasons_bull.append(
            "5M trend rising"
        )
    elif ema20_5 < ema20_5_previous:
        bear += 1
        reasons_bear.append(
            "5M trend falling"
        )

    # 4. 5M RSI
    if 50 <= rsi5 <= 68:
        bull += 1
        reasons_bull.append(
            "5M RSI bullish zone"
        )
    elif 32 <= rsi5 < 50:
        bear += 1
        reasons_bear.append(
            "5M RSI bearish zone"
        )

    # 5. 5M MACD
    if macd5_hist > 0:
        bull += 1
        reasons_bull.append(
            "5M MACD positive"
        )
    elif macd5_hist < 0:
        bear += 1
        reasons_bear.append(
            "5M MACD negative"
        )

    # ========================================================
    # 1M CONDITIONS
    # ========================================================

    # 6. Price vs EMA9
    if price1 > ema9_1:
        bull += 1
        reasons_bull.append(
            "1M price above EMA9"
        )
    elif price1 < ema9_1:
        bear += 1
        reasons_bear.append(
            "1M price below EMA9"
        )

    # 7. EMA9 vs EMA21
    if ema9_1 > ema21_1:
        bull += 1
        reasons_bull.append(
            "1M EMA9 above EMA21"
        )
    elif ema9_1 < ema21_1:
        bear += 1
        reasons_bear.append(
            "1M EMA9 below EMA21"
        )

    # 8. 1M MACD
    if macd1_hist > 0:
        bull += 1
        reasons_bull.append(
            "1M MACD positive"
        )
    elif macd1_hist < 0:
        bear += 1
        reasons_bear.append(
            "1M MACD negative"
        )

    # 9. 1M RSI
    if 52 <= rsi1 <= 68:
        bull += 1
        reasons_bull.append(
            "1M RSI confirms CALL"
        )
    elif 32 <= rsi1 <= 48:
        bear += 1
        reasons_bear.append(
            "1M RSI confirms PUT"
        )

    # 10. 1M momentum
    if price1 > previous1:
        bull += 1
        reasons_bull.append(
            "1M momentum up"
        )
    elif price1 < previous1:
        bear += 1
        reasons_bear.append(
            "1M momentum down"
        )

    # 11. Avoid extreme RSI entries
    call_allowed = rsi1 < 70 and rsi5 < 70
    put_allowed = rsi1 > 30 and rsi5 > 30

    signal = "⚪ NO TRADE"
    score = max(
        bull,
        bear,
    )
    reasons = []

    # Strict V2 entry
    if (
        bull >= 10
        and bull > bear
        and call_allowed
    ):
        signal = "🟢 CALL"
        score = bull
        reasons = reasons_bull

    elif (
        bear >= 10
        and bear > bull
        and put_allowed
    ):
        signal = "🔴 PUT"
        score = bear
        reasons = reasons_bear

    return {
        "symbol": symbol,
        "price": price5,

        "rsi5": rsi5,
        "rsi1": rsi1,

        "ema20": ema20_5,
        "ema50": ema50_5,

        "macd5": macd5_hist,
        "macd1": macd1_hist,

        "bull": bull,
        "bear": bear,

        "score": score,
        "signal": signal,

        "reasons": reasons,
    }


# ============================================================
# SIGNAL ID
# ============================================================

def make_signal_id(symbol):
    now = datetime.now(timezone.utc)

    return (
        f"{symbol}-"
        f"{now.strftime('%d%H%M')}-"
        f"{now.strftime('%S')}"
    )


# ============================================================
# TELEGRAM RESULT COMMANDS
# ============================================================

def get_telegram_updates(tracker):
    offset = tracker.get(
        "telegram_update_offset",
        0,
    )

    try:
        data = request_json(
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/getUpdates",
            {
                "offset": offset + 1,
                "timeout": 5,
            },
        )

    except Exception as error:
        print(
            f"Telegram update error: {error}"
        )
        return

    if not data.get("ok"):
        return

    updates = data.get(
        "result",
        [],
    )

    for update in updates:
        update_id = update.get(
            "update_id"
        )

        if update_id is not None:
            tracker[
                "telegram_update_offset"
            ] = update_id

        message = update.get(
            "message",
            {},
        )

        chat = message.get(
            "chat",
            {},
        )

        chat_id = str(
            chat.get("id", "")
        )

        # Only accept commands from the configured chat.
        if chat_id != TELEGRAM_CHAT_ID:
            continue

        text = str(
            message.get(
                "text",
                "",
            )
        ).strip()

        if not text:
            continue

        lower = text.lower()

        # ------------------------------------------
        # /win SIGNAL-ID
        # ------------------------------------------

        if lower.startswith("/win "):
            signal_id = text[5:].strip()

            record_result(
                tracker,
                signal_id,
                "WIN",
            )

        # ------------------------------------------
        # /loss SIGNAL-ID
        # ------------------------------------------

        elif lower.startswith("/loss "):
            signal_id = text[6:].strip()

            record_result(
                tracker,
                signal_id,
                "LOSS",
            )

        # ------------------------------------------
        # /stats
        # ------------------------------------------

        elif lower == "/stats":
            send_telegram(
                build_dashboard(
                    tracker
                )
            )


def record_result(
    tracker,
    signal_id,
    result,
):
    found = False

    for signal in tracker["signals"]:

        if signal.get("id") != signal_id:
            continue

        if signal.get("result") != "PENDING":
            continue

        signal["result"] = result

        signal["result_time"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        found = True

        break

    if found:
        save_tracker(tracker)

        send_telegram(
            f"✅ <b>RESULT RECORDED</b>\n\n"
            f"Signal: "
            f"<code>{html.escape(signal_id)}</code>\n"
            f"Result: <b>{result}</b>\n\n"
            f"Tracker updated."
        )

        print(
            f"Recorded {result}: "
            f"{signal_id}"
        )

    else:
        send_telegram(
            f"⚠️ Signal not found or already settled:\n"
            f"<code>{html.escape(signal_id)}</code>"
        )


# ============================================================
# PERFORMANCE
# ============================================================

def completed_signals(tracker):
    return [
        s
        for s in tracker["signals"]
        if s.get("result")
        in ("WIN", "LOSS")
    ]


def calculate_stats(tracker):
    signals = completed_signals(
        tracker
    )

    wins = sum(
        1
        for s in signals
        if s["result"] == "WIN"
    )

    losses = sum(
        1
        for s in signals
        if s["result"] == "LOSS"
    )

    total = wins + losses

    return {
        "signals": total,
        "wins": wins,
        "losses": losses,
        "rate": (
            wins / total * 100
            if total
            else 0
        ),
    }


def category_stats(
    signals,
    field,
):
    output = {}

    values = sorted(
        set(
            s.get(field)
            for s in signals
            if s.get(field) is not None
        )
    )

    for value in values:

        selected = [
            s
            for s in signals
            if s.get(field) == value
            and s.get("result")
            in ("WIN", "LOSS")
        ]

        wins = sum(
            1
            for s in selected
            if s["result"] == "WIN"
        )

        losses = sum(
            1
            for s in selected
            if s["result"] == "LOSS"
        )

        total = wins + losses

        if total:
            output[value] = {
                "wins": wins,
                "losses": losses,
                "rate": wins / total * 100,
                "total": total,
            }

    return output


def hour_stats(signals):
    output = {}

    for signal in signals:

        if signal.get("result") not in (
            "WIN",
            "LOSS",
        ):
            continue

        try:
            hour = datetime.fromisoformat(
                signal["time"]
            ).hour

        except Exception:
            continue

        key = f"{hour:02d}:00"

        if key not in output:
            output[key] = {
                "wins": 0,
                "losses": 0,
            }

        if signal["result"] == "WIN":
            output[key]["wins"] += 1
        else:
            output[key]["losses"] += 1

    for key in output:
        total = (
            output[key]["wins"]
            + output[key]["losses"]
        )

        output[key]["total"] = total

        output[key]["rate"] = (
            output[key]["wins"]
            / total
            * 100
        )

    return output


def build_dashboard(tracker):
    signals = completed_signals(
        tracker
    )

    overall = calculate_stats(
        tracker
    )

    call = [
        s
        for s in signals
        if s["signal"] == "CALL"
    ]

    put = [
        s
        for s in signals
        if s["signal"] == "PUT"
    ]

    call_wins = sum(
        1
        for s in call
        if s["result"] == "WIN"
    )

    put_wins = sum(
        1
        for s in put
        if s["result"] == "WIN"
    )

    call_rate = (
        call_wins / len(call) * 100
        if call
        else 0
    )

    put_rate = (
        put_wins / len(put) * 100
        if put
        else 0
    )

    scores = category_stats(
        signals,
        "score",
    )

    assets = category_stats(
        signals,
        "symbol",
    )

    hours = hour_stats(
        signals
    )

    best_asset = None
    worst_asset = None
    best_hour = None

    if assets:
        eligible = [
            (k, v)
            for k, v in assets.items()
            if v["total"] >= 3
        ]

        if eligible:
            best_asset = max(
                eligible,
                key=lambda x: x[1]["rate"]
            )

            worst_asset = min(
                eligible,
                key=lambda x: x[1]["rate"]
            )

    if hours:
        eligible_hours = [
            (k, v)
            for k, v in hours.items()
            if v["total"] >= 3
        ]

        if eligible_hours:
            best_hour = max(
                eligible_hours,
                key=lambda x: x[1]["rate"]
            )

    pending = sum(
        1
        for s in tracker["signals"]
        if s.get("result") == "PENDING"
    )

    legacy = tracker[
        "legacy_baseline"
    ]

    total_experiment = (
        legacy["signals"]
        + overall["signals"]
    )

    total_wins = (
        legacy["wins"]
        + overall["wins"]
    )

    total_losses = (
        legacy["losses"]
        + overall["losses"]
    )

    total_rate = (
        total_wins
        / total_experiment
        * 100
        if total_experiment
        else 0
    )

    message = (
        "🧠 <b>PRECISION SCANNER V2</b>\n\n"

        "📊 <b>NEW TRACKER</b>\n"
        f"Signals: <b>{overall['signals']}</b>\n"
        f"Wins: <b>{overall['wins']}</b>\n"
        f"Losses: <b>{ov
