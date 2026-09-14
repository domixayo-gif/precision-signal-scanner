import os
import json
import html
import requests
import subprocess
from datetime import datetime, timezone

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])

BASE = "https://api.exchange.coinbase.com"
TRACKER = "tracker.json"

ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "ADA", "TRX", "LINK",
    "TON", "AVAX", "DOGE", "DOT", "LTC", "POL"
]

BASELINE = {
    "total": 100,
    "wins": 58,
    "losses": 42,
    "call_wins": 31,
    "call_losses": 19,
    "put_wins": 27,
    "put_losses": 23,
    "scores": {"8": 48, "9": 55, "10": 63, "11": 68},
    "best": "LINK OTC",
    "worst": "ETH OTC"
}


def api(url, params=None):
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def send(text):
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        },
        timeout=20
    )
    r.raise_for_status()


def load_tracker():
    if not os.path.exists(TRACKER):
        return {"signals": [], "offset": 0}

    try:
        with open(TRACKER, encoding="utf-8") as f:
            data = json.load(f)

        data.setdefault("signals", [])
        data.setdefault("offset", 0)
        return data
    except Exception:
        return {"signals": [], "offset": 0}


def save_tracker(data):
    with open(TRACKER, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def ema(values, period):
    if len(values) < period:
        raise ValueError("not enough EMA data")

    value = sum(values[:period]) / period
    k = 2 / (period + 1)

    for price in values[period:]:
        value = price * k + value * (1 - k)

    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        raise ValueError("not enough RSI data")

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        gain = (gain * (period - 1) + gains[i]) / period
        loss = (loss * (period - 1) + losses[i]) / period

    if loss == 0:
        return 100.0

    return 100 - 100 / (1 + gain / loss)


def macd(values):
    if len(values) < 60:
        raise ValueError("not enough MACD data")

    lines = []

    for i in range(26, len(values) + 1):
        fast = ema(values[:i], 12)
        slow = ema(values[:i], 26)
        lines.append(fast - slow)

    line = lines[-1]
    signal = ema(lines, 9)

    return line, signal, line - signal


def get_closes(product, seconds):
    url = BASE + "/products/" + product + "/candles"
    data = api(url, {"granularity": seconds})

    if not isinstance(data, list) or len(data) < 61:
        raise ValueError("insufficient candle data")

    data.sort(key=lambda x: x[0])

    closes = [float(x[4]) for x in data]

    # Ignore newest candle because it may still be forming.
    closes = closes[:-1]

    return closes[-200:]


def get_markets():
    data = api(BASE + "/products")
    markets = {}

    for item in data:
        if item.get("status") != "online":
            continue

        base = item.get("base_currency")
        quote = item.get("quote_currency")

        if base not in ASSETS:
            continue

        if quote not in ("USD", "USDC"):
            continue

        if base not in markets:
            markets[base] = item["id"]

    return markets


def analyze(symbol, product):
    m5 = get_closes(product, 300)
    m1 = get_closes(product, 60)

    price5 = m5[-1]
    price1 = m1[-1]

    ema20 = ema(m5, 20)
    ema50 = ema(m5, 50)
    old_ema20 = ema(m5[:-3], 20)
    rsi5 = rsi(m5)
    _, _, macd5 = macd(m5)

    ema9 = ema(m1, 9)
    ema21 = ema(m1, 21)
    rsi1 = rsi(m1)
    _, _, macd1 = macd(m1)

    bull = 0
    bear = 0

    if price5 > ema20:
        bull += 2
    elif price5 < ema20:
        bear += 2

    if ema20 > ema50:
        bull += 2
    elif ema20 < ema50:
        bear += 2

    if ema20 > old_ema20:
        bull += 1
    elif ema20 < old_ema20:
        bear += 1

    if 50 <= rsi5 < 70:
        bull += 1
    elif 30 < rsi5 < 50:
        bear += 1

    if macd5 > 0:
        bull += 1
    elif macd5 < 0:
        bear += 1

    if ema9 > ema21:
        bull += 1
    elif ema9 < ema21:
        bear += 1

    if macd1 > 0:
        bull += 1
    elif macd1 < 0:
        bear += 1

    if 50 <= rsi1 < 70:
        bull += 1
    elif 30 < rsi1 < 50:
        bear += 1

    if price1 > m1[-2]:
        bull += 1
    elif price1 < m1[-2]:
        bear += 1

    signal = "NO TRADE"
    score = max(bull, bear)

    call_ok = (
        bull >= 10
        and bull > bear
        and price5 > ema20
        and ema20 > ema50
        and ema20 > old_ema20
        and price1 > ema9
        and ema9 > ema21
        and rsi5 < 70
        and rsi1 < 70
        and macd5 > 0
        and macd1 > 0
    )

    put_ok = (
        bear >= 10
        and bear > bull
        and price5 < ema20
        and ema20 < ema50
        and ema20 < old_ema20
        and price1 < ema9
        and ema9 < ema21
        and rsi5 > 30
        and rsi1 > 30
        and macd5 < 0
        and macd1 < 0
    )

    if call_ok:
        signal = "CALL"
    elif put_ok:
        signal = "PUT"

    return {
        "symbol": symbol,
        "price": price5,
        "rsi5": rsi5,
        "rsi1": rsi1,
        "score": score,
        "signal": signal
    }


def winrate(items):
    if not items:
        return 0.0

    wins = 0

    for item in items:
        if item["result"] == "WIN":
            wins += 1

    return wins * 100 / len(items)


def process_updates(data):
    try:
        url = "https://api.telegram.org/bot" + TOKEN + "/getUpdates"
        result = api(
            url,
            {
                "offset": data["offset"] + 1,
                "timeout": 2
            }
        )
    except Exception as error:
        print("Telegram update error:", error)
        return

    for update in result.get("result", []):
        data["offset"] = update["update_id"]

        message = update.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))

        if chat_id != CHAT_ID:
            continue

        text = str(message.get("text", "")).strip()
        parts = text.split()

        if len(parts) == 1 and parts[0].lower() == "/stats":
            send(stats(data))
            continue

        if len(parts) != 2:
            continue

        command = parts[0].lower()

        if command not in ("/win", "/loss"):
            continue

        signal_id = parts[1]
        result_value = "WIN" if command == "/win" else "LOSS"
        found = None

        for signal in data["signals"]:
            if signal["id"] == signal_id:
                found = signal
                break

        if found is None:
            send(
                "⚠️ Signal not found: <code>"
                + html.escape(signal_id)
                + "</code>"
            )
            continue

        if found["result"] != "PENDING":
            send(
                "<code>"
                + html.escape(signal_id)
                + "</code> is already <b>"
                + html.escape(found["result"])
                + "</b>."
            )
            continue

        found["result"] = result_value
        found["result_time"] = datetime.now(
            timezone.utc
        ).isoformat()

        send(
            "✅ <b>RESULT RECORDED</b>\n"
            "Signal: <code>"
            + html.escape(signal_id)
            + "</code>\n"
            "Result: <b>"
            + result_value
            + "</b>"
        )


def stats(data):
    done = []

    for signal in data["signals"]:
        if signal["result"] in ("WIN", "LOSS"):
            done.append(signal)

    wins = sum(x["result"] == "WIN" for x in done)
    losses = len(done) - wins

    pending = 0

    for signal in data["signals"]:
        if signal["result"] == "PENDING":
            pending += 1

    lines = [
        "📊 <b>PRECISION SCANNER V2</b>",
        "",
        "<b>NEW V2 TRACKER</b>",
        "Signals: " + str(len(done)),
        "Wins: " + str(wins),
        "Losses: " + str(losses),
        "Win rate: %.1f%%" % winrate(done),
        "Pending: " + str(pending),
        ""
    ]

    for side in ("CALL", "PUT"):
        group = []

        for signal in done:
            if signal["signal"] == side:
                group.append(signal)

        sw = sum(x["result"] == "WIN" for x in group)
        sl = sum(x["result"] == "LOSS" for x in group)

        lines.append("<b>" + side + "</b>")
        lines.append(
            str(sw)
            + "W / "
            + str(sl)
            + "L = "
            + "%.1f%%" % winrate(group)
        )

    lines += ["", "<b>SCORE PERFORMANCE</b>"]

    for score in (11, 10, 9, 8):
        group = []

        for signal in done:
            if signal["score"] == score:
                group.append(signal)

        if group:
            text = "%.1f%% (%d)" % (
                winrate(group),
                len(group)
            )
        else:
            text = "no data"

        lines.append(
            str(score) + "/11 → " + text
        )

    asset_groups = {}

    for signal in done:
        name = signal["symbol"]

        if name not in asset_groups:
            asset_groups[name] = []

        asset_groups[name].append(signal)

    eligible = []

    for name in asset_groups:
        if len(asset_groups[name]) >= 3:
            eligible.append(
                (name, asset_groups[name])
            )

    lines += ["", "<b>ASSET PERFORMANCE</b>"]

    if eligible:
        eligible.sort(
            key=lambda item: winrate(item[1]),
            reverse=True
        )

        for name, group in eligible:
            lines.append(
                name
                + " OTC → "
                + "%.1f%% (%d)" % (
                    winrate(group),
                    len(group)
                )
            )

        lines.append(
            "Best asset: <b>"
            + eligible[0][0]
            + " OTC</b>"
        )

        lines.append(
            "Worst asset: <b>"
            + eligible[-1][0]
            + " OTC</b>"
        )
    else:
        lines.append(
            "Need 3 completed trades per asset."
        )

    hour_groups = {}

    for signal in done:
        try:
            hour = datetime.fromisoformat(
                signal["time"]
            ).hour

            if hour not in hour_groups:
                hour_groups[hour] = []

            hour_groups[hour].append(signal)
        except Exception:
            pass

    hours = []

    for hour in hour_groups:
        if len(hour_groups[hour]) >= 3:
            hours.append(
                (hour, hour_groups[hour])
            )

    lines += ["", "<b>TIME PERFORMANCE</b>"]

    if hours:
        hours.sort(
            key=lambda item: winrate(item[1]),
            reverse=True
        )

        for hour, group in hours:
            lines.append(
                "%02d:00 UTC → %.1f%% (%d)" % (
                    hour,
                    winrate(group),
                    len(group)
                )
            )

        lines.append(
            "Best period: <b>%02d:00 UTC</b>"
            % hours[0][0]
        )

        lines.append(
            "Worst period: <b>%02d:00 UTC</b>"
            % hours[-1][0]
        )
    else:
        lines.append(
            "Need 3 completed trades per UTC hour."
        )

    combined_total = BASELINE["total"] + len(done)
    combined_wins = BASELINE["wins"] + wins
    combined_losses = BASELINE["losses"] + losses

    lines += [
        "",
        "<b>100-TRADE BASELINE</b>",
        "58W / 42L = 58%",
        "CALL: 31W / 19L = 62%",
        "PUT: 27W / 23L = 54%",
        "8/11: 48%",
        "9/11: 55%",
        "10/11: 63%",
        "11/11: 68%",
        "Best asset: LINK OTC",
        "Worst asset: ETH OTC",
        "",
        "<b>COMBINED EXPERIMENT</b>",
        "Signals: " + str(combined_total),
        "Wins: " + str(combined_wins),
        "Losses: " + str(combined_losses),
        "Win rate: %.1f%%"
        % (combined_wins * 100 / combined_total),
        "",
        "⚠️ Demo only.",
        "Coinbase is a proxy for Pocket Option OTC."
    ]

    return "\n".join(lines)


def build_report(results, errors, data):
    now = datetime.now(timezone.utc)

    lines = [
        "🧠 <b>PRECISION SCANNER V2</b>",
        "",
        "Scan: " + now.strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),
        "Data source: Coinbase proxy",
        "Primary timeframe: 5M",
        "Confirmation timeframe: 1M",
        "Expiry: <b>5 MINUTES</b>",
        "Minimum score: <b>10/11</b>",
        ""
    ]

    trades = []

    for result in results:
        if result["signal"] != "NO TRADE":
            trades.append(result)

    if not trades:
        lines += [
            "⚪ <b>NO QUALIFIED SIGNALS</b>",
            "",
            "No setup currently meets the V2 rules."
        ]
    else:
        lines += [
            "🔥 <b>QUALIFIED SIGNALS</b>",
            ""
        ]

        trades.sort(
            key=lambda item: item["score"],
            reverse=True
        )

        existing = set()

        for signal in data["signals"]:
            existing.add(signal["id"])

        for number, result in enumerate(trades, 1):
            signal_id = (
                result["symbol"]
                + "-"
                + now.strftime("%H%M%S")
            )

            if signal_id in existing:
                continue

            data["signals"].append({
                "id": signal_id,
                "symbol": result["symbol"],
                "signal": result["signal"],
                "score": result["score"],
                "time": now.isoformat(),
                "price": result["price"],
                "result": "PENDING",
                "result_time": None
            })

            existing.add(signal_id)

            lines += [
                "<b>#"
                + str(number)
                + " "
                + result["symbol"]
                + " OTC</b>",
                "🎯 <b>"
                + result["signal"]
                + "</b>",
                "Strength: <b>"
                + str(result["score"])
                + "/11</b>",
                "Price: %.6f" % result["price"],
                "5M RSI: %.1f" % result["rsi5"],
                "1M RSI: %.1f" % result["rsi1"],
                "Signal ID: <code>"
                + signal_id
                + "</code>",
                "Expiry: <b>5 MINUTES</b>",
                ""
            ]

    if errors:
        lines += ["⚠️ <b>UNAVAILABLE MARKETS</b>"]

        for error in errors:
            lines.append(
                "• " + html.escape(error)
            )

    lines += [
        "",
        "Use /win SIGNAL-ID",
        "Use /loss SIGNAL-ID",
        "Use /stats",
        "",
        "⚠️ Demo only. No guaranteed results."
    ]

    return "\n".join(lines)


def commit_tracker():
    subprocess.run(
        [
            "git",
            "config",
            "user.name",
            "github-actions[bot]"
        ],
        check=True
    )

    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]"
            "@users.noreply.github.com"
        ],
        check=True
    )

    subprocess.run(
        ["git", "add", TRACKER],
        check=True
    )

    check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"]
    )

    if check.returncode == 0:
        print("No tracker changes.")
        return

    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            "Update scanner tracker"
        ],
        check=True
    )

    subprocess.run(
        ["git", "push"],
        check=True
    )

    print("Tracker committed.")


def main():
    print("Starting scanner")

    data = load_tracker()

    print("Processing Telegram commands")
    process_updates(data)

    print("Loading Coinbase markets")
    found = get_markets()

    results = []
    errors = []

    for symbol in ASSETS:
        print("Analyzing " + symbol)

        if symbol not in found:
            errors.append(
                symbol + ": Coinbase market unavailable"
            )
            continue

        try:
            result = analyze(
                symbol,
                found[symbol]
            )

            results.append(result)

            print(
                "Signal: "
                + symbol
                + " "
                + result["signal"]
                + " "
                + str(result["score"])
                + "/11"
            )

        except Exception as error:
            errors.append(
                symbol + ": " + str(error)
            )

            print(
                "Error: "
                + symbol
                + " "
                + str(error)
            )

    if not results:
        raise RuntimeError(
            "All markets failed."
        )

    save_tracker(data)

    message = build_report(
        results,
        errors,
        data
    )

    save_tracker(data)

    print("Sending Telegram report")

    if len(message) <= 3900:
        send(message)
    else:
        start = 0

        while start < len(message):
            send(message[start:start + 3900])
            start += 3900

    save_tracker(data)

    print("Updating GitHub tracker")
    commit_tracker()

    print("Scanner complete")


if __name__ == "__main__":
    main()
