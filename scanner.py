import os
import html
import json
import subprocess
from datetime import datetime, timezone

import requests


TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])

BASE_URL = "https://api.exchange.coinbase.com"
TRACKER_FILE = "tracker.json"
TIMEOUT = 20


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


def api_get(url, params=None):
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
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )

    response.raise_for_status()


def default_tracker():
    return {
        "offset": 0,
        "signals": [],
        "baseline": {
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

        data.setdefault("offset", 0)
        data.setdefault("signals", [])
        data.setdefault(
            "baseline",
            default_tracker()["baseline"],
        )

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

    relative_strength = (
        average_gain / average_loss
    )

    return 100 - (
        100 / (1 + relative_strength)
    )


def macd(values):
    if len(values) < 60:
        raise RuntimeError(
            "Not enough candles for MACD"
        )

    macd_values = []

    for i in range(26, len(values) + 1):
        fast = ema(
            values[:i],
            12,
        )

        slow = ema(
            values[:i],
            26,
        )

        macd_values.append(
            fast - slow
        )

    signal = ema(
        macd_values,
        9,
    )

    if signal is None:
        raise RuntimeError(
            "Not enough MACD data"
        )

    line = macd_values[-1]
    histogram = line - signal

    return line, signal, histogram


def get_products():
    data = api_get(
        f"{BASE_URL}/products"
    )

    products = {}

    for product in data:
        if product.get("status") != "online":
            continue

        base = product.get(
            "base_currency"
        )

        quote = product.get(
            "quote_currency"
        )

        if (
            base in ASSETS
            and quote in ("USD", "USDC")
        ):
            products.setdefault(
                base,
                product["id"],
            )

    return products


def get_closes(product_id, granularity):
    data = api_get(
        f"{BASE_URL}/products/"
        f"{product_id}/candles",
        {
            "granularity": granularity,
        },
    )

    if not isinstance(data, list):
        raise RuntimeError(
            "Invalid candle response"
        )

    if len(data) < 60:
        raise RuntimeError(
            "Insufficient candles"
        )

    data.sort(
        key=lambda candle: candle[0]
    )

    closes = [
        float(candle[4])
        for candle in data
    ]

    # Ignore newest candle because it may
    # still be forming.
    if len(closes) > 60:
        closes = closes[:-1]

    return closes[-200:]


def analyze(symbol, product_id):
    five_min = get_closes(
        product_id,
        300,
    )

    one_min = get_closes(
        product_id,
        60,
    )

    price_5m = five_min[-1]
    price_1m = one_min[-1]

    previous_1m = one_min[-2]

    ema20_5m = ema(
        five_min,
        20,
    )

    ema50_5m = ema(
        five_min,
        50,
    )

    previous_ema20_5m = ema(
        five_min[:-3],
        20,
    )

    rsi_5m = rsi(
        five_min
    )

    _, _, macd_hist_5m = macd(
        five_min
    )

    ema9_1m = ema(
        one_min,
        9,
    )

    ema21_1m = ema(
        one_min,
        21,
    )

    rsi_1m = rsi(
        one_min
    )

    _, _, macd_hist_1m = macd(
        one_min
    )

    # The requested checks total 12 if every
    # condition receives its own point.
    # To keep the system genuinely 11/11,
    # the 1M price/EMA9/EMA21 conditions are
    # treated as ONE combined trend-alignment point.
    bullish_5m = [
        price_5m > ema20_5m,
        ema20_5m > ema50_5m,
        ema20_5m > previous_ema20_5m,
        50 <= rsi_5m <= 68,
        macd_hist_5m > 0,
    ]

    bearish_5m = [
        price_5m < ema20_5m,
        ema20_5m < ema50_5m,
        ema20_5m < previous_ema20_5m,
        32 <= rsi_5m <= 50,
        macd_hist_5m < 0,
    ]

    bullish_1m_alignment = (
        price_1m > ema9_1m
        and ema9_1m > ema21_1m
    )

    bearish_1m_alignment = (
        price_1m < ema9_1m
        and ema9_1m < ema21_1m
    )

    bullish_score = sum(
        [
            2 if bullish_5m[0] else 0,
            2 if bullish_5m[1] else 0,
            1 if bullish_5m[2] else 0,
            1 if bullish_5m[3] else 0,
            1 if bullish_5m[4] else 0,
            1 if bullish_1m_alignment else 0,
            1 if macd_hist_1m > 0 else 0,
            1 if 52 <= rsi_1m <= 68 else 0,
            1 if price_1m > previous_1m else 0,
        ]
    )

    bearish_score = sum(
        [
            2 if bearish_5m[0] else 0,
            2 if bearish_5m[1] else 0,
            1 if bearish_5m[2] else 0,
            1 if bearish_5m[3] else 0,
            1 if bearish_5m[4] else 0,
            1 if bearish_1m_alignment else 0,
            1 if macd_hist_1m < 0 else 0,
            1 if 32 <= rsi_1m <= 48 else 0,
            1 if price_1m < previous_1m else 0,
        ]
    )

    signal = "NO TRADE"
    score = max(
        bullish_score,
        bearish_score,
    )

    if (
        bullish_score >= 10
        and bullish_score > bearish_score
        and rsi_5m < 70
        and rsi_1m < 70
        and bullish_1m_alignment
    ):
        signal = "CALL"

    elif (
        bearish_score >= 10
        and bearish_score > bullish_score
        and rsi_5m > 30
        and rsi_1m > 30
        and bearish_1m_alignment
    ):
        signal = "PUT"

    return {
        "symbol": symbol,
        "price": price_5m,
        "rsi5": rsi_5m,
        "rsi1": rsi_1m,
        "bull": bullish_score,
        "bear": bearish_score,
        "score": score,
        "signal": signal,
    }


def make_signal_id(symbol, now):
    return (
        f"{symbol}-"
        f"{now.strftime('%y%m%d%H%M%S')}"
    )


def completed_signals(tracker):
    return [
        signal
        for signal in tracker["signals"]
        if signal.get("result")
        in ("WIN", "LOSS")
    ]


def calculate_rate(items):
    if not items:
        return None

    wins = sum(
        item["result"] == "WIN"
        for item in items
    )

    return wins / len(items) * 100


def record_command(tracker, text):
    parts = text.split()

    if len(parts) != 2:
        return False

    command = parts[0].lower()

    if command not in (
        "/win",
        "/loss",
    ):
        return False

    signal_id = parts[1]

    result = (
        "WIN"
        if command == "/win"
        else "LOSS"
    )

    for signal in tracker["signals"]:
        if signal["id"] != signal_id:
            continue

        if signal["result"] != "PENDING":
            send_telegram(
                f"⚠️ <b>"
                f"{html.escape(signal_id)}"
                f"</b> is already "
                f"<b>{signal['result']}</b>."
            )
            return True

        signal["result"] = result

        signal["result_time"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        send_telegram(
            "✅ <b>RESULT RECORDED</b>\n\n"
            f"Signal: <code>"
            f"{html.escape(signal_id)}"
            f"</code>\n"
            f"Result: <b>{result}</b>"
        )

        return True

    send_telegram(
        "⚠️ Signal not found:\n"
        f"<code>{html.escape(signal_id)}</code>"
    )

    return True


def process_telegram_updates(tracker):
    try:
        data = api_get(
            f"https://api.telegram.org/"
            f"bot{TOKEN}/getUpdates",
            {
                "offset": (
                    tracker["offset"] + 1
                ),
                "timeout": 3,
            },
        )

    except Exception as error:
        print(
            "Telegram update error:",
            error,
        )
        return

    for update in data.get(
        "result",
        [],
    ):
        tracker["offset"] = update[
            "update_id"
        ]

        message = update.get(
            "message",
            {},
        )

        chat_id = str(
            message.get(
                "chat",
                {},
            ).get(
                "id",
                "",
            )
        )

        if chat_id != CHAT_ID:
            continue

        text = str(
            message.get(
                "text",
                "",
            )
        ).strip()

        if not text:
            continue

        if text.lower() == "/stats":
            send_telegram(
                build_dashboard(
                    tracker
                )
            )

        else:
            record_command(
                tracker,
                text,
            )


def build_dashboard(tracker):
    signals = completed_signals(
        tracker
    )

    wins = sum(
        signal["result"] == "WIN"
        for signal in signals
    )

    losses = len(signals) - wins

    calls = [
        signal
        for signal in signals
        if signal["signal"] == "CALL"
    ]

    puts = [
        signal
        for signal in signals
        if signal["signal"] == "PUT"
    ]

    score_groups = {}

    for score in (
        8,
        9,
        10,
        11,
    ):
        score_groups[score] = [
            signal
            for signal in signals
            if signal["score"] == score
        ]

    asset_groups = {}

    for signal in signals:
        asset_groups.setdefault(
            signal["symbol"],
            [],
        ).append(signal)

    hour_groups = {}

    for signal in signals:
        try:
            hour = datetime.fromisoformat(
                signal["time"]
            ).hour
        except Exception:
            continue

        hour_groups.setdefault(
            hour,
            [],
        ).append(signal)

    eligible_assets = [
        (name, items)
        for name, items
        in asset_groups.items()
        if len(items) >= 3
    ]

    eligible_hours = [
        (hour, items)
        for hour, items
        in hour_groups.items()
        if len(items) >= 3
    ]

    best_asset = None
    worst_asset = None
    best_hour = None
    worst_hour = None

    if eligible_assets:
        best_asset = max(
            eligible_assets,
            key=lambda item: calculate_rate(
                item[1]
            ),
        )

        worst_asset = min(
            eligible_assets,
            key=lambda item: calculate_rate(
                item[1]
            ),
        )

    if eligible_hours:
        best_hour = max(
            eligible_hours,
            key=lambda item: calculate_rate(
                item[1]
            ),
        )

        worst_hour = min(
            eligible_hours,
            key=lambda item: calculate_rate(
                item[1]
            ),
        )

    pending = sum(
        signal.get("result") == "PENDING"
        for signal in tracker["signals"]
    )

    baseline = tracker["baseline"]

    combined_total = (
        baseline["signals"]
        + len(signals)
    )

    combined_wins = (
        baseline["wins"]
        + wins
    )

    combined_losses = (
        baseline["losses"]
        + losses
    )

    combined_rate = (
        combined_wins
        / combined_total
        * 100
        if combined_total
        else 0
    )

    lines = [
        "🧠 <b>PRECISION SCANNER V2</b>",
        "",
        "📊 <b>NEW V2 TRACKER</b>",
        f"Signals: <b>{len(signals)}</b>",
        f"Wins: <b>{wins}</b>",
        f"Losses: <b>{losses}</b>",
        f"Win rate: "
        f"<b>{calculate_rate(signals) or 0:.1f}%</b>",
        f"Pending: <b>{pending}</b>",
        "",
        "📞 <b>CALL</b>",
        f"{sum(s['result'] == 'WIN' for s in calls)}W / "
        f"{sum(s['result'] == 'LOSS' for s in calls)}L = "
        f"<b>{calculate_rate(calls) or 0:.1f}%</b>",
        "",
        "📉 <b>PUT</b>",
        f"{sum(s['result'] == 'WIN' for s in puts)}W / "
        f"{sum(s['result'] == 'LOSS' for s in puts)}L = "
        f"<b>{calculate_rate(puts) or 0:.1f}%</b>",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🏆 <b>SCORE PERFORMANCE</b>",
    ]

    for score in (
        11,
        10,
        9,
        8,
    ):
        items = score_groups[score]

        if items:
            lines.append(
                f"{score}/11 → "
                f"<b>{calculate_rate(items):.1f}%</b> "
                f"({len(items)} trades)"
            )
        else:
            lines.append(
                f"{score}/11 → no data"
            )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━",
            "🏆 <b>ASSET PERFORMANCE</b>",
        ]
    )

    if asset_groups:
        ranked_assets = sorted(
            asset_groups.items(),
            key=lambda item: calculate_rate(
                item[1]
            ),
            reverse=True,
        )

        for symbol, items in ranked_assets:
            lines.append(
                f"{symbol} OTC → "
                f"<b>{calculate_rate(items):.1f}%</b> "
                f"({len(items)})"
            )
    else:
        lines.append(
            "No completed trades yet."
        )

    if best_asset:
        lines.append(
            f"\n🥇 Best asset: "
            f"<b>{best_asset[0]} OTC</b> "
            f"({calculate_rate(best_asset[1]):.1f}%)"
        )
    else:
        lines.append(
            "\n🥇 Best asset: need 3 trades"
        )

    if worst_asset:
        lines.append(
            f"⚠️ Worst asset: "
            f"<b>{worst_asset[0]} OTC</b> "
            f"({calculate_rate(worst_asset[1]):.1f}%)"
        )
    else:
        lines.append(
            "⚠️ Worst asset: need 3 trades"
        )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━",
            "⏰ <b>TIME PERFORMANCE</b>",
        ]
    )

    if eligible_hours:
        ranked_hours = sorted(
            eligible_hours,
            key=lambda item: calculate_rate(
                item[1]
            ),
            reverse=True,
        )

        for hour, items in ranked_hours:
            lines.append(
                f"{hour:02d}:00 UTC → "
                f"<b>{calculate_rate(items):.1f}%</b> "
                f"({len(items)} trades)"
            )

        lines.append(
            f"\n🥇 Best period: "
            f"<b>{best_hour[0]:02d}:00 UTC</b>"
        )

        lines.append(
            f"⚠️ Worst period: "
            f"<b>{worst_hour[0]:02d}:00 UTC</b>"
        )
    else:
        lines.append(
            "Need at least 3 trades "
            "in a period."
        )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━",
            "📚 <b>100-TRADE BASELINE</b>",
            f"Historical: "
            f"<b>{baseline['wins']}W / "
            f"{baseline['losses']}L = 58%</b>",
            "CALL: 62%",
            "PUT: 54%",
            "8/11: 48%",
            "9/11: 55%",
            "10/11: 63%",
            "11/11: 68%",
            "Best: LINK OTC",
            "Worst: ETH OTC",
            "",
            "📈 <b>COMBINED EXPERIMENT</b>",
            f"Signals: <b>{combined_total}</b>",
            f"Wins: <b>{combined_wins}</b>",
            f"Losses: <b>{combined_losses}</b>",
            f"Win rate: "
            f"<b>{combined_rate:.1f}%</b>",
            "",
            "━━━━━━━━━━━━━━━━━━",
            "📌 <b>COMMANDS</b>",
            "<code>/win SIGNAL-ID</code>",
            "<code>/loss SIGNAL-ID</code>",
            "<code>/stats</code>",
            "",
            "⚠️ Demo testing only. "
            "No guarantee of profit.",
        ]
    )

    return "\n".join(lines)


def build_report(
    results,
    errors,
    tracker,
):
    now = datetime.now(
        timezone.utc
    )

    trades = [
        result
        for result in results
        if result["signal"] != "NO TRADE"
    ]

    trades.sort(
        key=lambda result: result["score"],
        reverse=True,
    )

    lines = [
        "🚨 <b>PRECISION SCANNER V2</b>",
        "",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "📡 Coinbase proxy data",
        "📊 Primary: 5M trend",
        "⚡ Confirmation: 1M",
        "⌛ Expiry: <b>5 MINUTES</b>",
        "🎯 Minimum signal: <b>10/11</b>",
        "⚠️ Coinbase is a proxy for Pocket Option OTC.",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if not trades:
        lines.extend(
            [
                "⚪ <b>NO QUALIFIED SIGNAL</b>",
                "",
                "The V2 filter rejected "
                "the current conditions.",
            ]
        )

    else:
        lines.extend(
            [
                "🔥 <b>QUALIFIED SIGNALS</b>",
                "",
            ]
        )

        for rank, result in enumerate(
            trades[:5],
            start=1,
        ):
            signal_id = make_signal_id(
                result["symbol"],
                now,
            )

            # Prevent duplicate IDs from being stored.
            existing_ids = {
                signal["id"]
         
