import os
import time
import logging
from datetime import datetime, timezone

from iqoptionapi.stable_api import IQ_Option

from storage import read_tracker, update_tracker


# ============================================================
# CONFIG
# ============================================================

IQ_EMAIL = os.environ["IQ_EMAIL"]
IQ_PASSWORD = os.environ["IQ_PASSWORD"]

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

EXPIRY_MINUTES = 5

MIN_SCORE = 80

COOLDOWN_MINUTES = 5

# Start with the instrument we have actually confirmed
# from the user's IQ Option account.
PREFERRED_OTC = [
    "EURUSD-OTC",
]

# If True, discover additional OTC instruments from IQ Option.
AUTO_DISCOVER_OTC = True

# Limit keeps GitHub Actions execution reasonable.
MAX_OTC_ASSETS = 30

# IQ Option data request sizes.
CANDLE_COUNT_5M = 120
CANDLE_COUNT_1M = 120

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("precision-scanner")


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    """
    Connect to IQ Option using GitHub Secrets.

    IMPORTANT:
    This scanner does NOT place trades.
    """

    logger.info("Connecting to IQ Option...")

    api = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    api.set_max_reconnect(3)

    connected = api.connect()

    if not connected:
        raise RuntimeError(
            "IQ Option connection failed. "
            "Check IQ_EMAIL/IQ_PASSWORD and account authentication."
        )

    # Always use practice/demo account.
    try:
        api.change_balance("PRACTICE")
    except Exception as exc:
        logger.warning(
            "Could not explicitly select PRACTICE account: %s",
            exc,
        )

    if not api.check_connect():
        raise RuntimeError(
            "IQ Option reports that the connection is not active."
        )

    logger.info("IQ Option connection established.")

    return api


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM = (
    "https://api.telegram.org/bot"
    + TELEGRAM_TOKEN
)


def send_message(text):
    import requests

    response = requests.post(
        TELEGRAM + "/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
        },
        timeout=30,
    )

    response.raise_for_status()


# ============================================================
# IQ OPTION OTC DISCOVERY
# ============================================================

def discover_otc_assets(api):
    """
    Ask IQ Option which OTC instruments are currently available.

    The API's get_all_open_time() response can contain sections
    such as digital, turbo and binary.

    We only collect names containing '-OTC'.

    No symbol is invented from Coinbase or another exchange.
    """

    discovered = set()

    try:
        logger.info("Discovering IQ Option OTC instruments...")

        all_open = api.get_all_open_time()

        if not isinstance(all_open, dict):
            logger.warning(
                "IQ Option returned an unexpected asset structure."
            )
            return []

        # OTC instruments may appear in multiple option sections.
        for section_name in (
            "digital",
            "turbo",
            "binary",
        ):
            section = all_open.get(section_name, {})

            if not isinstance(section, dict):
                continue

            for asset_name, info in section.items():

                if not isinstance(asset_name, str):
                    continue

                if "-OTC" not in asset_name.upper():
                    continue

                is_open = False

                if isinstance(info, dict):
                    is_open = bool(
                        info.get("open", False)
                    )

                if is_open:
                    discovered.add(asset_name)

        # Always retain our explicitly confirmed instrument if
        # IQ Option reports it as available.
        result = sorted(discovered)

        logger.info(
            "Discovered %d open OTC instruments.",
            len(result),
        )

        return result[:MAX_OTC_ASSETS]

    except Exception as exc:
        logger.exception(
            "OTC discovery failed: %s",
            exc,
        )

        return []


def get_assets(api):
    """
    Build the final asset list.

    Preferred assets are included only if they can be verified
    by the IQ Option open-time data.

    If discovery fails completely, EURUSD-OTC is still attempted
    as the explicitly confirmed instrument from the user's account.
    """

    discovered = discover_otc_assets(api)

    if discovered:
        assets = discovered

        # Make sure the confirmed EURUSD OTC instrument is not
        # accidentally removed by a future API ordering change.
        for preferred in PREFERRED_OTC:
            if preferred in discovered:
                assets.remove(preferred)
                assets.insert(0, preferred)

        return assets[:MAX_OTC_ASSETS]

    logger.warning(
        "No OTC instruments were discovered. "
        "Testing the confirmed EURUSD-OTC instrument directly."
    )

    return PREFERRED_OTC[:]


# ============================================================
# CANDLE DATA
# ============================================================

def normalize_candles(raw):
    """
    Convert IQ Option candle dictionaries into the row format
    used by the existing V3.5 indicator engine:

    [timestamp, low, high, open, close]
    """

    if not isinstance(raw, list):
        return []

    rows = []

    for candle in raw:

        if not isinstance(candle, dict):
            continue

        try:
            timestamp = int(
                candle.get("from", candle.get("at"))
            )

            open_price = float(candle["open"])
            close_price = float(candle["close"])
            high = float(candle["max"])
            low = float(candle["min"])

            rows.append(
                [
                    timestamp,
                    low,
                    high,
                    open_price,
                    close_price,
                ]
            )

        except (
            TypeError,
            ValueError,
            KeyError,
        ):
            continue

    rows.sort(key=lambda row: row[0])

    # Remove duplicate timestamps.
    cleaned = []
    seen = set()

    for row in rows:
        if row[0] in seen:
            continue

        seen.add(row[0])
        cleaned.append(row)

    return cleaned


def get_candles(api, symbol, interval, count):
    """
    Retrieve historical IQ Option candles.

    interval:
        60  = 1 minute
        300 = 5 minutes
    """

    try:
        endtime = int(
            api.get_server_timestamp()
        )

        raw = api.get_candles(
            symbol,
            interval,
            count,
            endtime,
        )

        rows = normalize_candles(raw)

        # The latest candle may still be forming.
        # Exclude it from the signal calculation.
        if len(rows) > 1:
            rows = rows[:-1]

        return rows

    except Exception as exc:
        logger.warning(
            "%s %sM candle error: %s",
            symbol,
            interval // 60,
            exc,
        )

        return []


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):

    if len(values) < period:
        return None

    total = sum(
        values[:period]
    )

    result = total / period

    multiplier = 2.0 / (period + 1.0)

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1.0 - multiplier)
        )

    return result


def rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0

    start = len(values) - period - 1

    for i in range(
        start,
        len(values) - 1,
    ):

        change = (
            values[i + 1]
            - values[i]
        )

        if change > 0:
            gains += change
        else:
            losses += abs(change)

    average_gain = gains / period
    average_loss = losses / period

    if average_loss == 0:
        return 100.0

    rs = (
        average_gain
        / average_loss
    )

    return 100.0 - (
        100.0 / (1.0 + rs)
    )


def true_ranges(rows):

    ranges = []

    for i in range(len(rows)):

        high = float(rows[i][2])
        low = float(rows[i][1])

        if i == 0:
            previous_close = float(
                rows[i][4]
            )
        else:
            previous_close = float(
                rows[i - 1][4]
            )

        first = high - low

        second = abs(
            high - previous_close
        )

        third = abs(
            low - previous_close
        )

        ranges.append(
            max(
                first,
                second,
                third,
            )
        )

    return ranges


def atr(rows, period=14):

    ranges = true_ranges(rows)

    if len(ranges) < period:
        return None

    return sum(
        ranges[-period:]
    ) / period


def macd(values):

    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return None

    return fast - slow


def momentum(values):

    if len(values) < 5:
        return 0

    recent = values[-5:]

    up = 0
    down = 0

    for i in range(
        1,
        len(recent),
    ):

        if recent[i] > recent[i - 1]:
            up += 1

        elif recent[i] < recent[i - 1]:
            down += 1

    if up > down:
        return 1

    if down > up:
        return -1

    return 0


def adx_dmi(rows, period=14):

    if len(rows) < period + 2:
        return None, None, None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(rows)):

        high = float(rows[i][2])
        low = float(rows[i][1])

        old_high = float(rows[i - 1][2])
        old_low = float(rows[i - 1][1])
        old_close = float(rows[i - 1][4])

        tr1 = high - low
        tr2 = abs(
            high - old_close
        )
        tr3 = abs(
            low - old_close
        )

        trs.append(
            max(tr1, tr2, tr3)
        )

        move_up = high - old_high
        move_down = old_low - low

        if (
            move_up > move_down
            and move_up > 0
        ):
            plus_dm.append(move_up)
        else:
            plus_dm.append(0.0)

        if (
            move_down > move_up
            and move_down > 0
        ):
            minus_dm.append(move_down)
        else:
            minus_dm.append(0.0)

    if len(trs) < period:
        return None, None, None

    recent_tr = trs[-period:]
    recent_plus = plus_dm[-period:]
    recent_minus = minus_dm[-period:]

    average_tr = (
        sum(recent_tr)
        / period
    )

    if average_tr == 0:
        return 0.0, 0.0, 0.0

    plus_di = (
        sum(recent_plus)
        / average_tr
        * 100.0
    )

    minus_di = (
        sum(recent_minus)
        / average_tr
        * 100.0
    )

    denominator = (
        plus_di + minus_di
    )

    if denominator == 0:
        dx = 0.0
    else:
        dx = (
            abs(
                plus_di
                - minus_di
            )
            / denominator
            * 100.0
        )

    return (
        dx,
        plus_di,
        minus_di,
    )


def structure(rows):

    if len(rows) < 12:
        return 0

    recent = rows[-6:]
    older = rows[-12:-6]

    recent_high = max(
        float(row[2])
        for row in recent
    )

    recent_low = min(
        float(row[1])
        for row in recent
    )

    older_high = max(
        float(row[2])
        for row in older
    )

    older_low = min(
        float(row[1])
        for row in older
    )

    if (
        recent_high > older_high
        and recent_low > older_low
    ):
        return 1

    if (
        recent_high < older_high
        and recent_low < older_low
    ):
        return -1

    return 0


# ============================================================
# ANALYSIS
# ============================================================

def analyze(api, symbol):

    candles_5m = get_candles(
        api,
        symbol,
        300,
        CANDLE_COUNT_5M,
    )

    candles_1m = get_candles(
        api,
        symbol,
        60,
        CANDLE_COUNT_1M,
    )

    if len(candles_5m) < 70:
        return None, "5M data unavailable"

    if len(candles_1m) < 50:
        return None, "1M data unavailable"

    prices5 = [
        float(row[4])
        for row in candles_5m
    ]

    prices1 = [
        float(row[4])
        for row in candles_1m
    ]

    price5 = prices5[-1]
    price1 = prices1[-1]

    ema20_5 = ema(prices5, 20)
    ema50_5 = ema(prices5, 50)
    ema20_old = ema(
        prices5[:-5],
        20,
    )

    ema9_1 = ema(prices1, 9)
    ema21_1 = ema(prices1, 21)

    rsi5 = rsi(prices5, 14)
    rsi1 = rsi(prices1, 14)

    macd5 = macd(prices5)
    macd5_old = macd(
        prices5[:-3]
    )

    macd1 = macd(prices1)
    macd1_old = macd(
        prices1[:-3]
    )

    atr5 = atr(
        candles_5m,
        14,
    )

    atr1 = atr(
        candles_1m,
        14,
    )

    adx5, plus_di, minus_di = adx_dmi(
        candles_5m,
        14,
    )

    struct = structure(
        candles_5m
    )

    values = [
        ema20_5,
        ema50_5,
        ema20_old,
        ema9_1,
        ema21_1,
        rsi5,
        rsi1,
        macd5,
        macd5_old,
        macd1,
        macd1_old,
        atr5,
        atr1,
        adx5,
        plus_di,
        minus_di,
    ]

    if any(
        value is None
        for value in values
    ):
        return None, "indicator error"

    if atr5 <= 0 or atr1 <= 0:
        return None, "invalid volatility"

    call = 0
    put = 0

    trend_score = 0
    structure_score = 0
    adx_score = 0
    macd_score = 0
    rsi_score = 0
    entry_score = 0
    pullback_score = 0
    candle_score = 0
    room_score = 0
    extension_score = 0

    # --------------------------------------------------------
    # 5M TREND
    # --------------------------------------------------------

    if price5 > ema20_5:
        trend_score += 10

    if ema20_5 > ema50_5:
        trend_score += 10

    if price5 < ema20_5:
        trend_score -= 10

    if ema20_5 < ema50_5:
        trend_score -= 10

    if trend_score > 0:
        call += 20

    elif trend_score < 0:
        put += 20

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    if struct == 1:
        call += 10
        structure_score = 10

    elif struct == -1:
        put += 10
        structure_score = 10

    # --------------------------------------------------------
    # ADX / DMI
    # --------------------------------------------------------

    if adx5 >= 15:

        if plus_di > minus_di:
            call += 10
            adx_score = 10

        elif minus_di > plus_di:
            put += 10
            adx_score = 10

        else:
            adx_score = 5

    elif adx5 >= 12:

        if plus_di > minus_di:
            call += 5
            adx_score = 5

        elif minus_di > plus_di:
            put += 5
            adx_score = 5

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    if (
        macd5 > 0
        and macd5 >= macd5_old
    ):
        call += 5
        macd_score += 5

    if (
        macd5 < 0
        and macd5 <= macd5_old
    ):
        put += 5
        macd_score += 5

    if (
        macd1 > 0
        and macd1 >= macd1_old
    ):
        call += 5
        macd_score += 5

    if (
        macd1 < 0
        and macd1 <= macd1_old
    ):
        put += 5
        macd_score += 5

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if 50 <= rsi5 < 70:
        call += 5
        rsi_score += 5

    if 48 <= rsi1 < 70:
        call += 5
        rsi_score += 5

    if 30 < rsi5 <= 50:
        put += 5
        rsi_score += 5

    if 30 < rsi1 <= 52:
        put += 5
        rsi_score += 5

    # --------------------------------------------------------
    # 1M ENTRY
    # --------------------------------------------------------

    if price1 > ema9_1:
        call += 7
        entry_score += 7

    if price1 < ema9_1:
        put += 7
        entry_score += 7

    if ema9_1 > ema21_1:
        call += 8
        entry_score += 8

    if ema9_1 < ema21_1:
        put += 8
        entry_score += 8

    # --------------------------------------------------------
    # EXTENSION
    # --------------------------------------------------------

    distance1 = abs(
        price1 - ema20_5
    )

    extension_ratio = (
        distance1 / atr5
        if atr5 > 0
        else 99.0
    )

    if extension_ratio <= 1.8:

        if call > put:
            call += 5
            extension_score = 5

        elif put > call:
            put += 5
            extension_score = 5

    # --------------------------------------------------------
    # CANDLE STRENGTH
    # --------------------------------------------------------

    last = candles_1m[-1]

    last_open = float(last[3])
    last_high = float(last[2])
    last_low = float(last[1])
    last_close = float(last[4])

    last_range = (
        last_high - last_low
    )

    body = abs(
        last_close - last_open
    )

    body_ratio = (
        body / last_range
        if last_range > 0
        else 0
    )

    if last_range <= atr1 * 1.8:

        if body_ratio >= 0.45:

            if last_close > last_open:
                call += 5
                candle_score = 5

            elif last_close < last_open:
                put += 5
                candle_score = 5

    # --------------------------------------------------------
    # ROOM
    # --------------------------------------------------------

    recent_low = min(
        float(row[1])
        for row in candles_5m[-12:]
    )

    recent_high = max(
        float(row[2])
        for row in candles_5m[-12:]
    )

    if call > put:

        room = (
            recent_high - price1
        )

        if room >= atr5 * 0.8:
            call += 5
            room_score = 5

    elif put > call:

        room = (
            price1 - recent_low
        )

        if room >= atr5 * 0.8:
            put += 5
            room_score = 5

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    recent_momentum = momentum(
        prices1
    )

    if recent_momentum == 1:
        call += 5
        pullback_score = 5

    elif recent_momentum == -1:
        put += 5
        pullback_score = 5

    # --------------------------------------------------------
    # DIRECTION
    # --------------------------------------------------------

    if call > put:
        direction = "CALL"
        score = call

    elif put > call:
        direction = "PUT"
        score = put

    else:
        return None, "score tied"

    # --------------------------------------------------------
    # HARD QUALIFICATION FILTERS
    # --------------------------------------------------------

    if direction == "CALL":

        if price5 <= ema20_5:
            return None, "bullish trend not confirmed"

        if ema20_5 <= ema50_5:
            return None, "5M bearish structure"

        if (
            plus_di <= minus_di
            and adx5 >= 15
        ):
            return None, "DMI conflict"

        if (
            rsi5 >= 72
            or rsi1 >= 75
        ):
            return None, "CALL RSI too high"

        if recent_momentum == -1:
            return None, "1M momentum conflict"

    if direction == "PUT":

        if price5 >= ema20_5:
            return None, "bearish trend not confirmed"

        if ema20_5 >= ema50_5:
            return None, "5M bullish structure"

        if (
            minus_di <= plus_di
            and adx5 >= 15
        ):
            return None, "DMI conflict"

        if (
            rsi5 <= 28
            or rsi1 <= 25
        ):
            return None, "PUT RSI too low"

        if recent_momentum == 1:
            return None, "1M momentum conflict"

    # --------------------------------------------------------
    # VOLATILITY FILTER
    # --------------------------------------------------------

    volatility_ratio = (
        atr1 / atr5
        if atr5 > 0
        else 99
    )

    if volatility_ratio > 0.55:
        return None, "extreme volatility"

    # --------------------------------------------------------
    # MINIMUM SCORE
    # --------------------------------------------------------

    if score < MIN_SCORE:
        return None, (
            "score "
            + str(score)
            + "/100"
        )

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    entry_ts = int(
        candles_1m[-1][0]
    )

    signal = {
        "symbol": symbol,
        "asset": symbol,
        "direction": direction,
        "score": score,
        "price": round(
            price1,
            8,
        ),
        "rsi5": round(
            rsi5,
            1,
        ),
        "rsi1": round(
            rsi1,
            1,
        ),
        "adx": round(
            adx5,
            1,
        ),
        "entry_ts": entry_ts,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "expiry_minutes": EXPIRY_MINUTES,
        "expiry_at": datetime.fromtimestamp(
            entry_ts
            + EXPIRY_MINUTES * 60,
            timezone.utc,
        ).isoformat(),
        "result": "PENDING",
    }

    return signal, None


# ============================================================
# TRACKER
# ============================================================

def cooldown_allowed(
    data,
    symbol,
    direction,
    entry_ts,
):

    cutoff = (
        entry_ts
        - COOLDOWN_MINUTES * 60
    )

    signals = data.get(
        "signals",
        [],
    )

    for old in reversed(signals):

        if old.get("symbol") != symbol:
            continue

        old_ts = int(
            old.get(
                "entry_ts",
                0,
            )
        )

        if old_ts < cutoff:
            break

        if old.get(
            "direction"
        ) == direction:
            return False

    return True


def save_signals(
    signals,
    scan_id,
):

    if not signals:
        return

    def mutate(data):

        existing = {
            old.get("id")
            for old in data.get(
                "signals",
                [],
            )
        }

        for signal in signals:

            if signal["id"] not in existing:
                data.setdefault(
                    "signals",
                    [],
                ).append(signal)

        data["signals"] = data[
            "signals"
        ][-1000:]

        return data

    update_tracker(
        mutate,
        "Add IQ Option signals "
        + scan_id,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    scan_id = os.getenv(
        "GITHUB_RUN_ID"
    )

    if not scan_id:
        scan_id = str(
            int(time.time())
        )

    api = None

    try:

        api = connect_iq()

        assets = get_assets(api)

        if not assets:
            raise RuntimeError(
                "No IQ Option OTC instruments were found."
            )

        data, _ = read_tracker()

        found = []
        rejected = []
        unavailable = []

        for symbol in assets:

            try:

                signal, reason = analyze(
                    api,
                    symbol,
                )

                if signal:

                    signal["id"] = (
                        "IQ5-"
                        + symbol.replace(
                            "-",
                            "",
                        )
                        + "-"
                        + signal[
                            "direction"
                        ]
                        + "-"
                        + str(
                            signal[
                                "entry_ts"
                            ]
                        )
                    )

                    if cooldown_allowed(
                        data,
                        symbol,
                        signal[
                            "direction"
                        ],
                        signal[
                            "entry_ts"
                        ],
                    ):

                        found.append(
                            signal
                        )

                    else:

                        rejected.append(
                            symbol
                            + ": cooldown"
                        )

                elif reason in (
                    "5M data unavailable",
                    "1M data unavailable",
                    "indicator error",
                    "invalid volatility",
                ):

                    unavailable.append(
                        symbol
                        + " ("
                        + str(reason)
                        + ")"
                    )

                else:

                    rejected.append(
                        symbol
                        + ": "
                        + str(reason)
                    )

            except Exception as exc:

                unavailable.append(
                    symbol
                    + " ("
                    + type(exc).__name__
                    + ": "
                    + str(exc)
                    + ")"
                )

        save_signals(
            found,
            scan_id,
        )

        now = datetime.now(
            timezone.utc
        )

        timestamp = (
            now.isoformat()
            .replace(
                "T",
                " ",
            )
            .replace(
                "+00:00",
                " UTC",
            )
        )

        lines = [
            "🟦 PRECISION SCANNER V4.0",
            "Scan: " + timestamp,
            "Data: IQ Option",
            "Market: IQ Option OTC",
            "Trend: 5M",
            "Entry: 1M",
            "Expiry: 5 MINUTES",
            "Minimum score: 80/100",
            "OTC assets analyzed: "
            + str(len(assets)),
            "",
        ]

        if found:

            lines.append(
                "🚨 QUALIFIED SIGNALS"
            )
            lines.append("")

            for signal in found:

                if signal[
                    "direction"
                ] == "CALL":

                    icon = "🟢 CALL"

                else:

                    icon = "🔴 PUT"

                lines.append(
                    icon
                    + " • "
                    + signal["asset"]
                )

                lines.append(
                    "Score: "
                    + str(
                        signal["score"]
                    )
                    + "/100"
                )

                lines.append(
                    "Price: "
                    + str(
                        signal["price"]
                    )
                )

                lines.append(
                    "ADX: "
                    + str(
                        signal["adx"]
                    )
                )

                lines.append(
                    "5M RSI: "
                    + str(
                        signal["rsi5"]
                    )
                    + " | 1M RSI: "
                    + str(
                        signal["rsi1"]
                    )
                )

                lines.append(
                    "Signal ID: "
                    + signal["id"]
                )

                lines.append(
                    "Reference expiry: 5 minutes"
                )

                lines.append("")

        else:

            lines.append(
                "⚪ NO TRADE"
            )

            lines.append(
                "No OTC asset passed "
                "the qualification filters."
            )

            if rejected:

                lines.append("")
                lines.append(
                    "Top rejection reasons:"
                )

                for item in rejected[:8]:

                    lines.append(
                        "• " + item
                    )

        if unavailable:

            lines.append("")
            lines.append(
                "⚠️ DATA WARNINGS"
            )

            for item in unavailable[:8]:

                lines.append(
                    "• " + item
                )

        lines.append("")
        lines.append(
            "Commands: /win ID | /loss ID | /stats"
        )

        lines.append("")
        lines.append(
            "⚠️ DEMO/TESTING ONLY."
        )

        lines.append(
            "This scanner does not place trades."
        )

        send_message(
            "\n".join(lines)
        )

    finally:

        if api is not None:

            try:
                api.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()

This keeps the original V3.5 indicator engine while changing the data source to IQ Option. The community API documents "get_candles()" and the OTC instrument naming/discovery pattern, including "EURUSD-OTC".

---

2. "requirements.txt"

Replace your current "requirements.txt" with:

:::writing{variant="document" id="41827" title="requirements.txt"}

requests>=2.28,<3
websocket-client>=1.5,<2
git+https://github.com/victalejo/iqoptionapi.git

I am deliberately using the newer community fork rather than the very old "websocket-client==0.56" dependency from the legacy implementation. The newer fork documents Python 3.10+ and current "websocket-client" compatibility.

---

3. GitHub Actions

Your workflow needs to expose the four secrets to Python.

Open:

".github/workflows/" → your scanner workflow ".yml" file

The important part should look like this:

name: Precision Signal Scanner

on:
  workflow_dispatch:
  schedule:
    - cron: "*/5 * * * *"

permissions:
  contents: write

jobs:
  scanner:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run IQ Option scanner
        env:
          IQ_EMAIL: ${{ secrets.IQ_EMAIL }}
          IQ_PASSWORD: ${{ secrets.IQ_PASSWORD }}
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          python scanner.py

Important

Your "TWELVE_DATA_API_KEY" doesn't need to be passed to this new scanner, because we're no longer using Twelve Data or Coinbase for the IQ Option OTC candles.

Also, the workflow deliberately has:

python-version: "3.11"

because the newer community IQ Option package currently specifies Python 3.10 or newer.

First run

Don't immediately judge the signals.

The first GitHub Actions run is a connection/data test:

GitHub Actions
     ↓
IQ_EMAIL + IQ_PASSWORD
     ↓
IQ Option connection
     ↓
Discover open OTC instruments
     ↓
EURUSD-OTC / other verified OTC assets
     ↓
5M + 1M candles
     ↓
Precision Scanner
     ↓
Telegram

If the connection works, the Telegram message should say something like:

🟦 PRECISION SCANNER V4.0
Data: IQ Option
Market: IQ Option OTC
OTC assets analyzed: X

If it cannot authenticate, retrieve candles, or discover the OTC instruments, don't change the strategy or lower the score. Send me the GitHub Actions error screenshot/log and we'll fix the connection layer.

One limitation to be aware of: this is based on an unofficial community IQ Option API, not an official IQ Option API, so authentication or candle access can stop working if IQ Option changes its platform.
