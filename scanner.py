import os
import csv
import json
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# ==================== SETTINGS ====================
MOMENTUM_PERIOD = 20
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.07
MIN_TURN_DISTANCE = 0.07
CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1
CANDLE_COUNT = 80
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25.0

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0
TARGET_TRADES = 50
DAILY_PROFIT_TARGET = 35.0
MAX_LOSING_GROUPS = 20
MAX_GROUP_TRADES = 3
RECOVERY_STAKES = [1.0, 2.0, 4.0]

SCAN_INTERVAL = 5
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
RESULT_RETRY_SECONDS = 1
LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"
LAGOS = ZoneInfo("Africa/Lagos")

EMAIL = os.getenv("IQ_EMAIL", "")
PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

api = None
crypto_assets = []
crypto_active_ids = {}
last_heartbeat = 0
last_signal_candles = {}

CRYPTO_SYMBOLS = {
    "BTC", "ETH", "LTC", "XRP", "BCH", "EOS", "ETC", "ADA", "DOT",
    "SOL", "DOGE", "TRX", "LINK", "XLM", "BNB", "DASH", "ZEC", "NEO",
    "QTUM", "IOTA", "ATOM", "AVAX", "SHIB", "UNI", "FIL", "APT", "ARB",
    "OP", "PEPE", "TON", "NEAR", "ICP", "SUI", "INJ", "AAVE", "ALGO",
    "VET", "FTM", "HBAR", "MANA", "SAND", "AXS", "CRV", "COMP", "MKR",
    "SNX", "YFI", "GRT", "EGLD", "RUNE", "THETA", "XTZ", "KAVA", "ZIL",
    "ENJ", "CHZ", "BAT", "1INCH", "IMX", "SEI", "TIA", "WIF", "BONK",
    "FLOKI", "ORDI", "JUP", "WLD"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    force=True
)


def log(message):
    logging.info(message)


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
    except Exception as exc:
        log("Telegram error: " + str(exc))


def new_state():
    return {
        "date": datetime.now(LAGOS).date().isoformat(),
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "group_wins": 0,
        "group_losses": 0,
        "group_active": False,
        "group_attempts": 0,
        "group_asset": "",
        "group_direction": "",
        "net_profit": 0.0
    }


def load_state():
    today = datetime.now(LAGOS).date().isoformat()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
        if state.get("date") == today:
            defaults = new_state()
            defaults.update(state)
            return defaults
    except (OSError, ValueError, TypeError):
        pass
    return new_state()


def save_state(state):
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
    os.replace(temp_file, STATE_FILE)


def write_csv(row):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if not exists:
            writer.writerow(
                ["time", "asset", "direction", "order_id", "outcome", "profit"]
            )
        writer.writerow(row)


def normalize_asset_name(name):
    text = str(name).strip().upper().replace("_", "-")
    if "." in text:
        text = text.split(".", 1)[1]
    return text


def is_crypto_name(name):
    normalized = normalize_asset_name(name)
    base = normalized.split("-")[0]
    return any(base.startswith(symbol) for symbol in CRYPTO_SYMBOLS)


# ==================== CRYPTO-ONLY DISCOVERY ====================
def discover_asset():
    global crypto_assets, crypto_active_ids
    crypto_assets = []
    crypto_active_ids = {}

    if api is None:
        log("Crypto discovery skipped: API is not connected.")
        return []

    try:
        log("Discovering crypto assets from binary/turbo data only...")
        raw_data = api.get_all_init_v2()
    except Exception as exc:
        log("Crypto discovery error: " + str(exc))
        return []

    if not isinstance(raw_data, dict):
        log("Crypto discovery returned no data; will retry.")
        return []

    market_data = raw_data.get("result", raw_data)
    if not isinstance(market_data, dict):
        log("Unexpected binary/turbo data structure.")
        return []

    found = set()
    for market_name in ("turbo", "binary"):
        market = market_data.get(market_name, {})
        if not isinstance(market, dict):
            continue
        actives = market.get("actives", {})
        if not isinstance(actives, dict):
            continue

        for active_id, details in actives.items():
            if not isinstance(details, dict):
                continue
            if details.get("enabled") is not True:
                continue
            if details.get("is_suspended") is True:
                continue

            asset_name = normalize_asset_name(details.get("name", ""))
            if not asset_name or not is_crypto_name(asset_name):
                continue
            try:
                numeric_id = int(active_id)
            except (TypeError, ValueError):
                continue

            found.add(asset_name)
            crypto_active_ids[asset_name] = numeric_id
            OP_code.ACTIVES[asset_name] = numeric_id

    crypto_assets = sorted(found)
    log("Open crypto option assets found: " + str(len(crypto_assets)))
    for asset_name in crypto_assets:
        log("CRYPTO FOUND: " + asset_name)
    if not crypto_assets:
        log("No eligible crypto binary/turbo options found. No trades will be placed.")
    return list(crypto_assets)


# ==================== CONNECTION ====================
def connect_iq():
    global api, crypto_assets, crypto_active_ids
    api = None
    crypto_assets = []
    crypto_active_ids = {}

    if not EMAIL or not PASSWORD:
        log("Missing IQ_EMAIL or IQ_PASSWORD secret.")
        return False

    try:
        log("Connecting to IQ Option...")
        new_api = IQ_Option(EMAIL, PASSWORD)
        result = new_api.connect()
        connected = bool(result[0]) if isinstance(result, tuple) else bool(result)
        if not connected or not new_api.check_connect():
            log("IQ Option connection failed.")
            return False

        new_api.change_balance(BALANCE_MODE)
        api = new_api
        log("Connected. Balance mode: " + BALANCE_MODE)

        assets = discover_asset()
        if not assets:
            log("No crypto assets confirmed; trading remains disabled until retry.")
            try:
                api.close_connect()
            except Exception:
                pass
            api = None
            return False

        telegram(
            "Crypto Momentum 20 scanner connected.\n"
            + "Mode: " + BALANCE_MODE + "\n"
            + "Crypto assets found: " + str(len(assets)) + "\n"
            + "Recovery: cross-pair, same direction\n"
            + "Stakes: $1 / $2 / $4"
        )
        return True
    except Exception as exc:
        log("Connection error: " + str(exc))
        api = None
        crypto_assets = []
        return False


# ==================== CANDLES ====================
def get_candles(asset):
    if api is None or not api.check_connect():
        return []
    try:
        candles = api.get_candles(
            asset, CANDLE_SECONDS, CANDLE_COUNT, time.time()
        )
        if not isinstance(candles, list):
            log("Invalid candle response for " + asset)
            return []

        now = time.time()
        completed = []
        for candle in candles:
            try:
                candle_end = float(candle.get("to", 0))
                if candle_end and candle_end < now:
                    completed.append(candle)
            except (TypeError, ValueError, AttributeError):
                continue

        completed.sort(key=lambda item: float(item.get("from", 0)))
        required = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2
        if len(completed) < required:
            log("Not enough completed candles for " + asset + ": " + str(len(completed)))
            return []
        return completed
    except Exception as exc:
        log("Candle error for " + asset + ": " + str(exc))
        return []


# ==================== MOMENTUM AND ADX ====================
def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def calculate_adx_di(candles, period=ADX_PERIOD):
    """Return ADX, +DI, -DI from completed OHLC candles using Wilder smoothing."""
    if len(candles) < (period * 2) + 2:
        return None
    try:
        highs = [float(c.get("max", c.get("high"))) for c in candles]
        lows = [float(c.get("min", c.get("low"))) for c in candles]
        closes = [float(c["close"]) for c in candles]
    except (KeyError, TypeError, ValueError):
        return None

    true_ranges = []
    plus_moves = []
    minus_moves = []
    for index in range(1, len(candles)):
        high = highs[index]
        low = lows[index]
        previous_close = closes[index - 1]
        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - low
        plus_move = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_move = down_move if down_move > up_move and down_move > 0 else 0.0
        true_ranges.append(true_range)
        plus_moves.append(plus_move)
        minus_moves.append(minus_move)

    if len(true_ranges) < period * 2:
        return None

    smoothed_tr = sum(true_ranges[:period])
    smoothed_plus = sum(plus_moves[:period])
    smoothed_minus = sum(minus_moves[:period])
    dx_values = []

    for index in range(period - 1, len(true_ranges)):
        if index >= period:
            smoothed_tr = smoothed_tr - smoothed_tr / period + true_ranges[index]
            smoothed_plus = smoothed_plus - smoothed_plus / period + plus_moves[index]
            smoothed_minus = smoothed_minus - smoothed_minus / period + minus_moves[index]

        if smoothed_tr <= 0:
            plus_di = 0.0
            minus_di = 0.0
        else:
            plus_di = 100.0 * smoothed_plus / smoothed_tr
            minus_di = 100.0 * smoothed_minus / smoothed_tr

        di_total = plus_di + minus_di
        dx = 0.0 if di_total <= 0 else 100.0 * abs(plus_di - minus_di) / di_total
        dx_values.append((dx, plus_di, minus_di))

    if len(dx_values) < period:
        return None
    adx = sum(item[0] for item in dx_values[:period]) / period
    for item in dx_values[period:]:
        adx = ((adx * (period - 1)) + item[0]) / period
    return adx, dx_values[-1][1], dx_values[-1][2]


def apply_adx_filter(direction, candles, asset):
    values = calculate_adx_di(candles, ADX_PERIOD)
    if values is None:
        log("ADX unavailable for " + asset + "; signal blocked for safety.")
        return False

    adx, plus_di, minus_di = values
    log(
        "ADX FILTER | " + asset
        + " | ADX=" + format(adx, ".2f")
        + " | +DI=" + format(plus_di, ".2f")
        + " | -DI=" + format(minus_di, ".2f")
    )
    if adx >= ADX_TREND_THRESHOLD:
        if direction == "put" and plus_di > minus_di:
            log("ADX BLOCK: PUT rejected against strong upward trend on " + asset)
            return False
        if direction == "call" and minus_di > plus_di:
            log("ADX BLOCK: CALL rejected against strong downward trend on " + asset)
            return False
    return True


def calculate_signal(candles, asset):
    if len(candles) < MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2:
        return None
    try:
        closes = [float(candle["close"]) for candle in candles]
    except (KeyError, TypeError, ValueError):
        log("Invalid candle close values for " + asset)
        return None

    momentums = []
    for index in range(MOMENTUM_PERIOD, len(closes)):
        previous_close = closes[index - MOMENTUM_PERIOD]
        if previous_close == 0:
            return None
        momentums.append(((closes[index] - previous_close) / previous_close) * 100.0)

    if len(momentums) < MOMENTUM_LOOKBACK + 2:
        return None

    current_candle = candles[-1]
    candle_id = int(current_candle.get("from", 0))
    if last_signal_candles.get(asset) == candle_id:
        return None

    history = momentums[-(MOMENTUM_LOOKBACK + 2):-2]
    if len(history) < MOMENTUM_LOOKBACK:
        return None

    low_limit = percentile(history, EXTREME_PERCENTILE)
    high_limit = percentile(history, 1.0 - EXTREME_PERCENTILE)
    previous_momentum = momentums[-2]
    current_momentum = momentums[-1]
    candle_open = float(current_candle["open"])
    candle_close = float(current_candle["close"])
    turn = current_momentum - previous_momentum
    previous_candle = candles[-2]

    try:
        previous_high = float(previous_candle.get("max", previous_candle.get("high", closes[-2])))
        previous_low = float(previous_candle.get("min", previous_candle.get("low", closes[-2])))
    except (TypeError, ValueError):
        log("Previous candle high/low unavailable for " + asset)
        last_signal_candles[asset] = candle_id
        return None

    call_setup = (
        previous_momentum <= low_limit
        and turn >= MIN_TURN_DISTANCE
        and candle_close > candle_open
    )
    put_setup = (
        previous_momentum >= high_limit
        and turn <= -MIN_TURN_DISTANCE
        and candle_close < candle_open
    )

    signal = None
    if call_setup and candle_close > previous_high:
        signal = "call"
        log("PRICE CONFIRMATION | " + asset + " | CALL broke previous candle high.")
    elif put_setup and candle_close < previous_low:
        signal = "put"
        log("PRICE CONFIRMATION | " + asset + " | PUT broke previous candle low.")
    elif call_setup:
        log("REVERSAL WAIT | " + asset + " | CALL did not break previous high.")
        last_signal_candles[asset] = candle_id
        return None
    elif put_setup:
        log("REVERSAL WAIT | " + asset + " | PUT did not break previous low.")
        last_signal_candles[asset] = candle_id
        return None

    if signal is None:
        return None
    if not apply_adx_filter(signal, candles, asset):
        last_signal_candles[asset] = candle_id
        return None

    last_signal_candles[asset] = candle_id
    return signal


# ==================== RISK CONTROLS ====================
def trading_allowed(state):
    if state["trades"] >= TARGET_TRADES:
        log("Individual trade limit reached.")
        return False
    if state["group_losses"] >= MAX_LOSING_GROUPS:
        log("Maximum losing groups reached: " + str(MAX_LOSING_GROUPS))
        return False
    if state["net_profit"] >= DAILY_PROFIT_TARGET:
        log("Daily profit target reached.")
        return False
    return True


def clear_group(state):
    state["group_active"] = False
    state["group_attempts"] = 0
    state["group_asset"] = ""
    state["group_direction"] = ""


# ==================== TRADE MANAGEMENT ====================
def place_trade(direction, state, asset):
    if not is_crypto_name(asset):
        log("SAFETY BLOCK: refused non-crypto asset " + str(asset))
        return False
    if not AUTO_TRADE:
        log("Signal only: " + asset + " " + direction.upper())
        telegram("Crypto signal: " + asset + " " + direction.upper())
        return False
    if api is None or not api.check_connect():
        log("Trade skipped. Broker disconnected.")
        return False
    if not trading_allowed(state):
        return False

    active_group = bool(state.get("group_active", False))
    required_direction = str(state.get("group_direction", "")).lower()
    if active_group and required_direction != direction.lower():
        log("SAFETY BLOCK: recovery must keep the original direction.")
        return False

    attempt_number = int(state.get("group_attempts", 0)) + 1
    if attempt_number > MAX_GROUP_TRADES:
        log("Recovery group reached its attempt limit; resetting group.")
        clear_group(state)
        save_state(state)
        return False

    stake = RECOVERY_STAKES[attempt_number - 1]
    if not active_group:
        state["group_active"] = True
        state["group_attempts"] = 0
        state["group_direction"] = direction.lower()
        state["group_asset"] = asset
        save_state(state)

    log(
        "TRADE | " + direction.upper()
        + " | Asset: " + asset
        + " | Stake: $" + format(stake, ".2f")
        + " | Group attempt: " + str(attempt_number)
        + "/" + str(MAX_GROUP_TRADES)
    )
    telegram(
        "Crypto trade\nAsset: " + asset
        + "\nDirection: " + direction.upper()
        + "\nStake: $" + format(stake, ".2f")
        + "\nGroup attempt: " + str(attempt_number) + "/" + str(MAX_GROUP_TRADES)
        + "\nExpiry: 1 minute"
    )

    try:
        accepted, order_id = api.buy(stake, asset, direction, EXPIRY_MINUTES)
    except Exception as exc:
        log("Order submission error: " + str(exc))
        if not active_group:
            clear_group(state)
            save_state(state)
        return False

    if not accepted or not order_id:
        log("Broker did not accept order for " + asset)
        if not active_group:
            clear_group(state)
            save_state(state)
        return False

    state["trades"] += 1
    state["group_attempts"] = attempt_number
    state["group_asset"] = asset
    save_state(state)
    log("Order accepted. ID: " + str(order_id))

    while True:
        try:
            result = api.check_win_v4(order_id)
            if result is None:
                time.sleep(RESULT_RETRY_SECONDS)
                continue
            if isinstance(result, (tuple, list)):
                if len(result) < 2 or result[1] is None:
                    log("Trade result pending: " + repr(result))
                    time.sleep(RESULT_RETRY_SECONDS)
                    continue
                log("Broker result status: " + str(result[0]).lower())
                result = float(result[1])
            else:
                result = float(result)
            break
        except Exception as exc:
            log("Result check failed: " + str(exc))
            time.sleep(RESULT_RETRY_SECONDS)
            if api is None or not api.check_connect():
                log("Connection lost while waiting for result; reconnecting.")
                if not connect_iq():
                    time.sleep(RECONNECT_SECONDS)

    if result > 0:
        profit = result
        state["wins"] += 1
        state["group_wins"] += 1
        outcome = "WIN"
        group_message = "GROUP WIN: recovery sequence finished; reset to $1."
        clear_group(state)
    else:
        # A zero result is counted as a full stake loss, matching the user's stats.
        profit = -stake if result == 0 else result
        state["losses"] += 1
        outcome = "LOSS"
        if attempt_number >= MAX_GROUP_TRADES:
            state["group_losses"] += 1
            group_message = (
                "GROUP LOSS: all three attempts lost. Maximum group stake loss: $7.00. "
                "Group reset; normal scanning resumes."
            )
            clear_group(state)
        else:
            state["group_active"] = True
            state["group_direction"] = direction.lower()
            state["group_asset"] = asset
            group_message = (
                "LOSS: scanning ALL eligible crypto pairs for a fresh "
                + direction.upper() + " signal. Next stake: $"
                + format(RECOVERY_STAKES[attempt_number], ".2f")
            )

    state["net_profit"] = round(state["net_profit"] + profit, 2)
    save_state(state)
    log(
        outcome + " | Asset: " + asset
        + " | Direction: " + direction.upper()
        + " | Stake: $" + format(stake, ".2f")
        + " | Result: " + format(result, ".2f")
        + " | Counted P/L: " + format(profit, ".2f")
        + " | Daily P/L: $" + format(state["net_profit"], ".2f")
    )
    log(group_message)
    write_csv([
        datetime.now(LAGOS).isoformat(), asset, direction, order_id,
        outcome, round(profit, 2)
    ])
    telegram(
        "Crypto trade " + outcome
        + "\nAsset: " + asset
        + "\nDirection: " + direction.upper()
        + "\nStake: $" + format(stake, ".2f")
        + "\nCounted P/L: $" + format(profit, ".2f")
        + "\nDaily P/L: $" + format(state["net_profit"], ".2f")
        + "\n" + group_message
        + "\nGroup wins: " + str(state["group_wins"])
        + " | Group losses: " + str(state["group_losses"])
    )
    return True


# ==================== MAIN SCANNER ====================
def run_scanner():
    global last_heartbeat, crypto_assets
    state = load_state()

    log("CRYPTO MOMENTUM 20 SCANNER STARTING")
    log("Balance mode: " + BALANCE_MODE)
    log("Automatic trading: " + str(AUTO_TRADE))
    log("Stake sequence: $1 / $2 / $4")
    log("Recovery: cross-pair, same direction, fresh signal required.")
    log("ADX filter: period " + str(ADX_PERIOD) + ", threshold " + str(ADX_TREND_THRESHOLD))
    log("Discovery: binary/turbo only; no forex fallback.")
    telegram(
        "Crypto Momentum 20 scanner starting.\n"
        + "Mode: " + BALANCE_MODE
        + "\nRecovery: cross-pair, same direction"
        + "\nStakes: $1 / $2 / $4"
        + "\nDaily profit target: $" + format(DAILY_PROFIT_TARGET, ".2f")
        + "\nMaximum losing groups: " + str(MAX_LOSING_GROUPS)
    )

    while True:
        try:
            today = datetime.now(LAGOS).date().isoformat()
            if state.get("date") != today:
                state = new_state()
                save_state(state)
                log("New Lagos trading day. Counters reset.")

            if api is None or not api.check_connect():
                log("Connecting or reconnecting...")
                if not connect_iq():
                    log("Connection/discovery retry in " + str(RECONNECT_SECONDS) + " seconds.")
                    time.sleep(RECONNECT_SECONDS)
                    continue

            if not trading_allowed(state):
                log("Trading paused by risk controls.")
                time.sleep(60)
                continue

            assets = list(crypto_assets)
            if not assets:
                assets = discover_asset()
            if not assets:
                log("No confirmed crypto assets. No trade will be placed.")
                time.sleep(RECONNECT_SECONDS)
                continue

            active_group = bool(state.get("group_active", False))
            required_direction = str(state.get("group_direction", "")).lower()
            if active_group and required_direction not in ("call", "put"):
                log("Invalid recovery direction; clearing unsafe group state.")
                clear_group(state)
                save_state(state)
                active_group = False

            if active_group:
                log(
                    "CROSS-PAIR RECOVERY | Scan all " + str(len(assets))
                    + " eligible crypto pairs for fresh " + required_direction.upper()
                    + " signal. Next attempt "
                    + str(int(state.get("group_attempts", 0)) + 1)
                    + "/" + str(MAX_GROUP_TRADES)
                )
            else:
                log("Scanning " + str(len(assets)) + " confirmed crypto assets.")

            trade_placed = False
            for asset in assets:
                if not trading_allowed(state):
                    break
                if not is_crypto_name(asset):
                    log("SAFETY BLOCK: skipped non-crypto asset " + str(asset))
                    continue
                if api is None or not api.check_connect():
                    log("Connection lost. Reconnecting.")
                    connect_iq()
                    break

                candles = get_candles(asset)
                if not candles:
                    continue
                signal = calculate_signal(candles, asset)
                if not signal:
                    continue

                # During recovery, only the original direction is permitted.
                # The pair may differ from the pair that produced the loss.
                if active_group and signal != required_direction:
                    log(
                        "RECOVERY SKIP | " + asset + " has " + signal.upper()
                        + "; waiting for " + required_direction.upper() + "."
                    )
                    continue

                if active_group:
                    log("CROSS-PAIR RECOVERY CONFIRMED | " + asset + " " + signal.upper())
                else:
                    log("Confirmed crypto signal: " + asset + " " + signal.upper())

                if place_trade(signal, state, asset):
                    trade_placed = True
                    break

            # One completed order per loop. During recovery, the next loop scans
            # all pairs again and uses the next $2/$4 stake after a losing result.
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                log(
                    "HEARTBEAT | Trades: " + str(state["trades"])
                    + " | Trade wins: " + str(state["wins"])
                    + " | Trade losses: " + str(state["losses"])
                    + " | Group wins: " + str(state["group_wins"])
                    + " | Group losses: " + str(state["group_losses"])
                    + "/" + str(MAX_LOSING_GROUPS)
                    + " | Daily P/L: $" + format(state["net_profit"], ".2f")
                    + " | Crypto assets: " + str(len(crypto_assets))
                )
                last_heartbeat = now
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("Scanner stopped by user.")
            break
        except Exception as exc:
            log("Scanner loop error: " + str(exc))
            time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run_scanner()
