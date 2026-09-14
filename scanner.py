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


def tg(method, data=None):
    url = "https://api.telegram.org/bot" + TOKEN + "/" + method
    r = requests.post(url, data=data or {}, timeout=20)
    r.raise_for_status()
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError(str(result))
    return result["result"]


def send(text):
    for i in range(0, len(text), 3900):
        tg("sendMessage", {
            "chat_id": CHAT_ID,
            "text": text[i:i + 3900],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true"
        })


def load():
    if not os.path.exists(TRACKER):
        return {"signals": [], "offset": 0}
    try:
        with open(TRACKER, encoding="utf-8") as f:
            x = json.load(f)
        if not isinstance(x, dict):
            raise ValueError()
        x.setdefault("signals", [])
        x.setdefault("offset", 0)
        return x
    except Exception:
        return {"signals": [], "offset": 0}


def save(data):
    with open(TRACKER, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def ema(v, p):
    if len(v) < p:
        raise ValueError("not enough EMA data")
    x = sum(v[:p]) / p
    k = 2 / (p + 1)
    for n in v[p:]:
        x = n * k + x * (1 - k)
    return x


def rsi(v, p=14):
    if len(v) < p + 1:
        raise ValueError("not enough RSI data")

    gains = []
    losses = []

    for i in range(1, len(v)):
        d = v[i] - v[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    gain = sum(gains[:p]) / p
    loss = sum(losses[:p]) / p

    for i in range(p, len(gains)):
        gain = (gain * (p - 1) + gains[i]) / p
        loss = (loss * (p - 1) + losses[i]) / p

    if loss == 0:
        return 100.0

    return 100 - 100 / (1 + gain / loss)


def macd(v):
    if len(v) < 60:
        raise ValueError("not enough MACD data")

    lines = []

    for i in range(26, len(v) + 1):
        lines.append(
            ema(v[:i], 12) - ema(v[:i], 26)
        )

    line = lines[-1]
    signal = ema(lines, 9)

    return line, signal, line - signal


def candles(product, seconds):
    data = api(
        BASE + "/products/" + product + "/candles",
        {"granularity": seconds}
    )

    if not isinstance(data, list) or len(data) < 61:
        raise ValueError("insufficient candles")

    data.sort(key=lambda x: x[0])
    values = [float(x[4]) for x in data]

    # Ignore newest candle.
    return values[:-1][-200:]


def markets():
    data = api(BASE + "/products")
    found = {}

    for item in data:
        if item.get("status") != "online":
            continue

        base = item.get("base_currency")
        quote = item.get("quote_currency")

        if base in ASSETS and quote in ("USD", "USDC"):
            if base not in found:
                found[base] = item["id"]

    return found


def analyze(symbol, product):
    m5 = candles(product, 300)
    m1 = candles(product, 60)

    p5 = m5[-1]
    p1 = m1[-1]

    e20 = ema(m5, 20)
    e50 = ema(m5, 50)
    old20 = ema(m5[:-3], 20)

    r5 = rsi(m5)
    _, _, h5 = macd(m5)

    e9 = ema(m1, 9)
    e21 = ema(m1, 21)
    r1 = rsi(m1)
    _, _, h1 = macd(m1)

    bull = 0
    bear = 0

    # 5M price vs EMA20: 2 points.
    if p5 > e20:
        bull += 2
    elif p5 < e20:
        bear += 2

    # 5M EMA20 vs EMA50: 2 points.
    if e20 > e50:
        bull += 2
    elif e20 < e50:
        bear += 2

    # 5M EMA20 slope: 1 point.
    if e20 > old20:
        bull += 1
    elif e20 < old20:
        bear += 1

    # 5M RSI: 1 point.
    if 50 <= r5 < 70:
        bull += 1
    elif 30 < r5 < 50:
        bear += 1

    # 5M MACD: 1 point.
    if h5 > 0:
        bull += 1
    elif h5 < 0:
        bear += 1

    # 1M price vs EMA9: 1 point.
    if p1 > e9:
        bull += 1
    elif p1 < e9:
        bear += 1

    # 1M EMA9 vs EMA21: 1 point.
    if e9 > e21:
        bull += 1
    elif e9 < e21:
        bear += 1

    # 1M MACD: 1 point.
    if h1 > 0:
        bull += 1
    elif h1 < 0:
        bear += 1

    # 1M momentum: 1 point.
    if p1 > m1[-2]:
        bull += 1
    elif p1 < m1[-2]:
        bear += 1

    signal = "NO TRADE"
    score = max(bull, bear)

    call = (
        bull >= 10
        and bull > bear
        and p5 > e20
        and e20 > e50
        and e20 > old20
        and h5 > 0
        and p1 > e9
        and e9 > e21
        and h1 > 0
        and p1 > m1[-2]
        and r5 < 70
        and r1 < 70
    )

    put = (
        bear >= 10
        and bear > bull
        and p5 < e20
        and e20 < e50
        and e20 < old20
        and h5 < 0
        and p1 < e9
        and e9 < e21
        and h1 < 0
        and p1 < m1[-2]
        and r5 > 30
        and r1 > 30
    )

    if call:
        signal = "CALL"
    elif put:
        signal = "PUT"

    return {
        "symbol": symbol,
        "price": p5,
        "rsi5": r5,
        "rsi1": r1,
        "score": score,
        "signal": signal
    }


def winrate(items):
    if not items:
        return 0.0

    wins = 0
    for x in items:
        if x["result"] == "WIN":
            wins += 1

    return wins * 100 / len(items)


def help_text():
    return (
        "🤖 <b>PRECISION SCANNER V2</b>\n\n"
        "Commands:\n"
        "/start - show help\n"
        "/win SIGNAL-ID - record WIN\n"
        "/loss SIGNAL-ID - record LOSS\n"
        "/stats - performance report\n\n"
        "Example:\n"
        "<code>/win LTC-131117</code>\n"
        "<code>/loss SOL-131117</code>\n\n"
        "⚠️ DEMO ONLY.\n"
        "Coinbase is a proxy for Pocket Option OTC."
    )


def process_commands(data):
    print("Checking Telegram commands...")

    try:
        updates = tg("getUpdates", {
            "offset": data["offset"] + 1,
            "limit": 100,
            "timeout": 1
        })
    except Exception as e:
        print("Telegram command error:", e)
        return

    changed = False

    for update in updates:
        data["offset"] = update["update_id"]

        msg = update.get("message", {})
        chat = msg.get("chat", {})

        if str(chat.get("id", "")) != CHAT_ID:
            continue

        text = str(msg.get("text", "")).strip()
        parts = text.split()

        if not parts:
            continue

        command = parts[0].split("@")[0].lower()

        if command == "/start" and len(parts) == 1:
            send(help_text())
            continue

        if command == "/stats" and len(parts) == 1:
            send(stats(data))
            continue

        if command in ("/win", "/loss"):
            if len(parts) != 2:
                send(
                    "⚠️ <b>Invalid command.</b>\n"
                    "Use <code>" + command +
                    " SIGNAL-ID</code>"
                )
                continue

            wanted = parts[1]
            found = None

            for item in data["signals"]:
                if item["id"] == wanted:
                    found = item
                    break

            if found is None:
                send(
                    "❌ Signal not found: <code>" +
                    html.escape(wanted) + "</code>"
                )
                continue

            if found["result"] != "PENDING":
                send(
                    "⚠️ <code>" +
                    html.escape(wanted) +
                    "</code> is already <b>" +
                    html.escape(found["result"]) +
                    "</b>."
                )
                continue

            value = "WIN" if command == "/win" else "LOSS"

            found["result"] = value
            found["result_time"] = (
                datetime.now(timezone.utc).isoformat()
            )

            changed = True

            send(
                "✅ <b>RESULT RECORDED</b>\n"
                "Signal: <code>" +
                html.escape(wanted) +
                "</code>\n"
                "Result: <b>" + value + "</b>"
            )
            continue

        if command.startswith("/"):
            send(
                "❓ Unknown command.\n\n" +
                help_text()
            )

    if changed:
        save(data)


def stats(data):
    done = []
    pending = 0

    for x in data["signals"]:
        if x["result"] in ("WIN", "LOSS"):
            done.append(x)
        elif x["result"] == "PENDING":
            pending += 1

    wins = sum(x["result"] == "WIN" for x in done)
    losses = len(done) - wins

    lines = [
        "📊 <b>PRECISION SCANNER V2</b>",
        "",
        "<b>NEW V2 TRACKER</b>",
        "Total: " + str(len(done)),
        "Wins: " + str(wins),
        "Losses: " + str(losses),
        "Win rate: %.1f%%" % winrate(done),
        "Pending: " + str(pending),
        ""
    ]

    for side in ("CALL", "PUT"):
        group = []

        for x in done:
            if x["signal"] == side:
                group.append(x)

        sw = sum(x["result"] == "WIN" for x in group)
        sl = len(group) - sw

        lines.append(
            side + ": " +
            str(sw) + "W / " +
            str(sl) + "L = " +
            "%.1f%%" % winrate(group)
        )

    lines += ["", "<b>SCORE PERFORMANCE</b>"]

    for score in (11, 10, 9, 8):
        group = []

        for x in done:
            if x["score"] == score:
                group.append(x)

        if group:
            lines.append(
                str(score) + "/11 → " +
                "%.1f%% (%d)" %
                (winrate(group), len(group))
            )
        else:
            lines.append(
                str(score) + "/11 → no data"
            )

    asset_groups = {}

    for x in done:
        name = x["symbol"]

        if name not in asset_groups:
            asset_groups[name] = []

        asset_groups[name].append(x)

    assets = []

    for name in asset_groups:
        if len(asset_groups[name]) >= 3:
            assets.append(
                (name, asset_groups[name])
            )

    lines += ["", "<b>ASSET PERFORMANCE</b>"]

    if assets:
        assets.sort(
            key=lambda x: winrate(x[1]),
            reverse=True
        )

        for name, group in assets:
            lines.append(
                name + " OTC → " +
                "%.1f%% (%d)" %
                (winrate(group), len(group))
            )

        lines.append(
            "Best asset: <b>" +
            assets[0][0] +
            " OTC</b>"
        )

        lines.append(
            "Worst asset: <b>" +
            assets[-1][0] +
            " OTC</b>"
        )
    else:
        lines.append(
            "Need 3 completed trades per asset."
        )

    hours = {}

    for x in done:
        try:
            hour = datetime.fromisoformat(
                x["time"]
            ).hour

            if hour not in hours:
                hours[hour] = []

            hours[hour].append(x)
        except Exception:
            pass

    good_hours = []

    for hour in hours:
        if len(hours[hour]) >= 3:
            good_hours.append(
                (hour, hours[hour])
            )

    lines += ["", "<b>TIME PERFORMANCE</b>"]

    if good_hours:
        good_hours.sort(
            key=lambda x: winrate(x[1]),
            reverse=True
        )

        for hour, group in good_hours:
            lines.append(
                "%02d:00 UTC → %.1f%% (%d)" %
                (hour, winrate(group), len(group))
            )

        lines.append(
            "Best period: <b>%02d:00 UTC</b>" %
            good_hours[0][0]
        )

        lines.append(
            "Worst period: <b>%02d:00 UTC</b>" %
            good_hours[-1][0]
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
        "8/11 = 48%",
        "9/11 = 55%",
        "10/11 = 63%",
        "11/11 = 68%",
        "Best asset: LINK OTC",
        "Worst asset: ETH OTC",
        "",
        "<b>COMBINED EXPERIMENT</b>",
        "Total: " + str(combined_total),
        "Wins: " + str(combined_wins),
        "Losses: " + str(combined_losses),
        "Win rate: %.1f%%" %
        (combined_wins * 100 / combined_total),
        "",
        "⚠️ DEMO ONLY.",
        "No guaranteed results."
    ]

    return "\n".join(lines)


def build_report(results, errors, data):
    now = datetime.now(timezone.utc)

    lines = [
        "🧠 <b>PRECISION SCANNER V2</b>",
        "",
        "Scan: " +
        now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "Data source: Coinbase proxy",
        "Primary timeframe: 5M",
        "Confirmation timeframe: 1M",
        "Expiry: <b>5 MINUTES</b>",
        "Minimum score: <b>10/11</b>",
        ""
    ]

    trades = []

    for x in results:
        if x["signal"] != "NO TRADE":
            trades.append(x)

    existing = set()

    for x in data["signals"]:
        existing.add(x["id"])

    if not trades:
        lines += [
            "⚪ <b>NO QUALIFIED SIGNALS</b>",
            "No setup meets all V2 rules."
        ]
    else:
        trades.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        number = 0

        for x in trades:
            signal_id = (
                x["symbol"] + "-" +
                now.strftime("%H%M%S")
            )

            if signal_id in existing:
                continue

            data["signals"].append({
                "id": signal_id,
                "symbol": x["symbol"],
                "signal": x["signal"],
                "score": x["score"],
                "time": now.isoformat(),
                "price": x["price"],
                "result": "PENDING",
                "result_time": None
            })

            existing.add(signal_id)
            number += 1

            lines += [
                "<b>#" + str(number) +
                " " + x["symbol"] + " OTC</b>",
                "🎯 <b>" + x["signal"] + "</b>",
                "Strength: <b>" +
                str(x["score"]) + "/11</b>",
                "Price: %.6f" % x["price"],
                "5M RSI: %.1f" % x["rsi5"],
                "1M RSI: %.1f" % x["rsi1"],
                "Signal ID: <code>" +
                signal_id + "</code>",
                "Expiry: <b>5 MINUTES</b>",
                ""
            ]

    if errors:
        lines.append(
            "⚠️ <b>UNAVAILABLE MARKETS</b>"
        )

        for error in errors:
            lines.append(
                "• " + html.escape(error)
            )

    lines += [
        "",
        "Commands:",
        "/start",
        "/win SIGNAL-ID",
        "/loss SIGNAL-ID",
        "/stats",
        "",
        "⚠️ DEMO ONLY.",
        "Coinbase is a proxy for Pocket Option OTC."
    ]

    return "\n".join(lines)


def commit():
    subprocess.run(
        [
            "git", "config", "user.name",
            "github-actions[bot]"
        ],
        check=True
    )

    subprocess.run(
        [
            "git", "config", "user.email",
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
            "git", "commit",
            "-m",
            "Update scanner tracker"
        ],
        check=True
    )

    subprocess.run(
        ["git", "push"],
        check=True
    )

    print("Tracker pushed successfully.")


def main():
    print("Starting scanner")

    data = load()

    print("Processing Telegram commands")
    process_commands(data)

    print("Loading Coinbase markets")
    found = markets()

    results = []
    errors = []

    for symbol in ASSETS:
        print("Analyzing " + symbol)

        if symbol not in found:
            errors.append(
                symbol +
                ": Coinbase market unavailable"
            )
            continue

        try:
            result = analyze(
                symbol,
                found[symbol]
            )

            results.append(result)

            print(
                symbol + ": " +
                result["signal"] + " " +
                str(result["score"]) + "/11"
            )

        except Exception as error:
            errors.append(
                symbol + ": " + str(error)
            )

            print(
                symbol + ": ERROR " +
                str(error)
            )

    if not results:
        raise RuntimeError(
            "All Coinbase markets failed."
        )

    message = build_report(
        results,
        errors,
        data
    )

    save(data)

    print("Sending Telegram report")
    send(message)

    print("Updating GitHub tracker")
    commit()

    print("Scanner complete")


if __name__ == "__main__":
    main()
