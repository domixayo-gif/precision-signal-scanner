import os
import re
import csv
import json
import time
import math
import logging
import threading
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# PRACTICE-ONLY scanner: forex + crypto pairs, including OTC when IQ Option exposes them.
# Strategy: Momentum 10 extreme-turn + candlestick pattern + separate closed follow-up candle.
IQ_EMAIL = os.getenv('IQ_EMAIL', '').strip()
IQ_PASSWORD = os.getenv('IQ_PASSWORD', '').strip()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '').strip()
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY', '').strip()
GITHUB_REF_NAME = os.getenv('GITHUB_REF_NAME', 'main').strip()
GITHUB_WORKFLOW_FILE = 'scanner.yml'

BALANCE_MODE = 'PRACTICE'
AUTO_TRADE = True
STAKE = 1.00
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60
MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.08
MIN_TURN_DISTANCE = 0.06
SESSION_PROFIT_TARGET = 5.0
SESSION_LOSS_LIMIT = 3.0
MAX_RESOLVED_TRADES = 20
MAX_CONSECUTIVE_LOSSES = 5
SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 20
CANDLE_REQUEST_TIMEOUT = 10
ORDER_RESULT_RETRY_SECONDS = 5
DIAGNOSTIC_LOG_SECONDS = 300
REJECTED_ASSET_COOLDOWN_SECONDS = 600
CANDLE_REQUEST_COUNT = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 15
LOG_FILE = 'momentum_signal_log.csv'
STATE_FILE = 'momentum_state.json'
LAGOS = ZoneInfo('Africa/Lagos')

CURRENCIES = {
    'USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'NZD', 'CNY',
    'HKD', 'SGD', 'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'HUF', 'TRY',
    'ZAR', 'MXN', 'BRL', 'INR', 'THB', 'AED', 'SAR', 'ILS', 'KRW',
    'TWD', 'RUB', 'RON', 'BGN', 'ISK', 'PHP', 'IDR', 'MYR'
}
CRYPTO_CODES = {
    'BTC', 'ETH', 'LTC', 'BCH', 'XRP', 'EOS', 'ETC', 'DASH', 'XLM',
    'TRX', 'ADA', 'DOT', 'SOL', 'DOGE', 'BNB', 'LINK', 'UNI', 'AVAX',
    'ATOM', 'NEO', 'ZEC', 'XMR', 'IOTA', 'XTZ', 'ALGO', 'FIL', 'APT',
    'ARB', 'OP', 'SHIB', 'TON', 'ICP', 'AAVE', 'MKR', 'SAND', 'MANA',
    'CHZ', 'ENJ', 'GALA', 'APE', 'FTM', 'NEAR', 'VET', 'QNT', 'COMP',
    'SNX', 'CRV', 'YFI', '1INCH', 'KAVA', 'RUNE', 'IMX', 'PEPE'
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
SESSION_ID = '{}-{}'.format(os.getenv('GITHUB_RUN_ID', str(int(time.time()))), os.getenv('GITHUB_RUN_ATTEMPT', '1'))
IQ = None
ASSETS = []
ASSET_REFRESH_AT = 0
REJECTED_ASSETS = {}
STOP_EVENT = threading.Event()
TRADE_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()
DIAG_LOCK = threading.Lock()
RESULT_MONITOR_LOCK = threading.Lock()
RESULT_MONITORS = {}
BETINFO_TASKS = {}
BETINFO_LAST_START = {}
NEXT_WORKFLOW_REQUESTED = False

DIAG_KEYS = [
    'assets_scanned', 'candle_data_ok', 'empty_candles', 'insufficient_candles',
    'signals_checked', 'patterns_found', 'followup_confirmed', 'followup_rejected',
    'm10_extreme_pass', 'm10_extreme_rejected', 'm10_turn_pass',
    'm10_turn_rejected', 'setups_found', 'duplicate_signals', 'order_rejected'
]
DIAG = {key: 0 for key in DIAG_KEYS}
DIAG_LAST_ASSET_LOG = {}


def now_lagos():
    return datetime.now(LAGOS)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        response = requests.post(
            'https://api.telegram.org/bot{}/sendMessage'.format(TELEGRAM_TOKEN),
            data={
                'chat_id': TELEGRAM_CHAT_ID, 'text': message,
                'parse_mode': 'HTML', 'disable_web_page_preview': True
            },
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        logging.warning('Telegram send failed: %s', exc)


def fresh_state():
    return {
        'date': now_lagos().date().isoformat(), 'session_id': SESSION_ID,
        'profit': 0.0, 'wins': 0, 'losses': 0, 'resolved': 0,
        'consecutive_losses': 0, 'pending_order_id': None,
        'pending_asset': None, 'pending_direction': None, 'pending_started': None,
        'last_signals': {}, 'stop_reason': '', 'settled_order_ids': []
    }


def load_state():
    state = fresh_state()
    try:
        with open(STATE_FILE, encoding='utf-8') as handle:
            old = json.load(handle)
        if not isinstance(old, dict):
            return state
    except (OSError, ValueError, TypeError):
        return state
    if old.get('date') == state['date']:
        for key in (
            'profit', 'wins', 'losses', 'resolved', 'consecutive_losses',
            'last_signals', 'stop_reason', 'settled_order_ids'
        ):
            if key in old:
                state[key] = old[key]
    for key in ('pending_order_id', 'pending_asset', 'pending_direction', 'pending_started'):
        state[key] = old.get(key)
    if not isinstance(state.get('last_signals'), dict):
        state['last_signals'] = {}
    if not isinstance(state.get('settled_order_ids'), list):
        state['settled_order_ids'] = []
    return state


STATE = load_state()


def persist_state_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logging.warning('State not committed: GITHUB_TOKEN/repository unavailable')
        return
    try:
        subprocess.run(
            ['git', 'config', 'user.name', 'github-actions[bot]'],
            check=True, capture_output=True, text=True
        )
        subprocess.run(
            ['git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com'],
            check=True, capture_output=True, text=True
        )
        files = [STATE_FILE] + ([LOG_FILE] if os.path.exists(LOG_FILE) else [])
        subprocess.run(['git', 'add'] + files, check=True, capture_output=True, text=True)
        if subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True).returncode == 0:
            return
        subprocess.run(
            ['git', 'commit', '-m', 'Persist scanner state and trade log'],
            check=True, capture_output=True, text=True
        )
        result = subprocess.run(['git', 'push', 'origin', 'HEAD'], capture_output=True, text=True)
        if result.returncode:
            logging.warning('State push failed: %s', result.stderr.strip())
    except Exception as exc:
        logging.warning('Git state persistence failed: %s', exc)


def save_state(persist=False):
    try:
        with STATE_LOCK:
            STATE['date'] = now_lagos().date().isoformat()
            STATE['session_id'] = SESSION_ID
            with open(STATE_FILE + '.tmp', 'w', encoding='utf-8') as handle:
                json.dump(STATE, handle, indent=2)
            os.replace(STATE_FILE + '.tmp', STATE_FILE)
        if persist:
            persist_state_to_github()
    except Exception as exc:
        logging.warning('State save failed: %s', exc)


def log_trade(asset, direction, m10, status, profit='', order_id=''):
    fields = [
        'timestamp_lagos', 'session_id', 'asset', 'direction',
        'momentum10', 'status', 'profit', 'order_id', 'session_profit'
    ]
    try:
        exists = os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0
        with open(LOG_FILE, 'a', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if not exists:
                writer.writeheader()
            writer.writerow({
                'timestamp_lagos': now_lagos().isoformat(timespec='seconds'),
                'session_id': SESSION_ID, 'asset': asset, 'direction': direction,
                'momentum10': '' if m10 is None else round(float(m10), 5),
                'status': status, 'profit': profit, 'order_id': order_id,
                'session_profit': round(float(STATE.get('profit', 0)), 2)
            })
    except Exception as exc:
        logging.warning('CSV logging error: %s', exc)


def inc(key, amount=1):
    with DIAG_LOCK:
        if key in DIAG:
            DIAG[key] += amount


def diag_notice(asset, message):
    if time.time() - DIAG_LAST_ASSET_LOG.get(asset, 0) >= DIAGNOSTIC_LOG_SECONDS:
        DIAG_LAST_ASSET_LOG[asset] = time.time()
        logging.warning('CANDLE DIAGNOSTIC %s: %s', asset, message)


def format_stats():
    return 'Daily P/L: ${:+.2f}\nWins: {}\nLosses: {}\nResolved trades: {}\nConsecutive losses: {}'.format(
        STATE['profit'], STATE['wins'], STATE['losses'], STATE['resolved'], STATE['consecutive_losses']
    )


def risk_stop_reason():
    profit = round(float(STATE.get('profit', 0)), 2)
    if profit >= SESSION_PROFIT_TARGET:
        return 'Daily take-profit reached (+${:.2f})'.format(SESSION_PROFIT_TARGET)
    if profit <= -SESSION_LOSS_LIMIT:
        return 'Daily loss limit reached (-${:.2f})'.format(SESSION_LOSS_LIMIT)
    if int(STATE.get('resolved', 0)) >= MAX_RESOLVED_TRADES:
        return 'Daily resolved-trade limit reached ({})'.format(MAX_RESOLVED_TRADES)
    if int(STATE.get('consecutive_losses', 0)) >= MAX_CONSECUTIVE_LOSSES:
        return 'Consecutive-loss limit reached ({})'.format(MAX_CONSECUTIVE_LOSSES)
    return None


def stop_if_limit_reached():
    reason = risk_stop_reason()
    if reason and not STOP_EVENT.is_set():
        STOP_EVENT.set()
        STATE['stop_reason'] = reason
        save_state(True)
        logging.warning('%s | %s', reason, format_stats())
        send_telegram('<b>SCANNER STOPPED</b>\n{}\n{}'.format(reason, format_stats()))
    return bool(reason)


def request_next_workflow():
    global NEXT_WORKFLOW_REQUESTED
    if NEXT_WORKFLOW_REQUESTED:
        return
    NEXT_WORKFLOW_REQUESTED = True
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logging.error('Cannot dispatch next workflow: missing token or repository')
        send_telegram('<b>Next workflow not triggered.</b> Check GITHUB_TOKEN permissions.')
        return
    url = 'https://api.github.com/repos/{}/actions/workflows/{}/dispatches'.format(
        GITHUB_REPOSITORY, GITHUB_WORKFLOW_FILE
    )
    headers = {
        'Authorization': 'Bearer ' + GITHUB_TOKEN,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }
    for attempt in range(3):
        try:
            response = requests.post(
                url, headers=headers, json={'ref': GITHUB_REF_NAME}, timeout=15
            )
            if response.status_code in (201, 204):
                send_telegram('<b>Next workflow queued.</b> Daily risk totals preserved.')
                return
            logging.warning(
                'Workflow dispatch failed: %s %s',
                response.status_code, response.text[:200]
            )
        except Exception as exc:
            logging.warning('Workflow dispatch error: %s', exc)
        time.sleep(3)
    send_telegram('<b>WARNING:</b> automatic workflow dispatch failed.')


def classify_asset_name(value):
    """Recognize currency pairs and crypto pairs from IQ Option's active names."""
    if not isinstance(value, str):
        return None, None, False
    raw = value.strip().upper().split('.')[-1].strip()
    if not raw:
        return None, None, False
    otc = bool(re.search(r'(^|[-_\s])OTC($|[-_\s])', raw)) or raw.endswith('OTC')
    cleaned = re.sub(r'[^A-Z0-9]', '', raw)
    for suffix in ('OTC', 'BINARY', 'TURBO', 'DIGITAL'):
        if cleaned.endswith(suffix):
            cleaned = cleaned[:-len(suffix)]
    # Strip common broker labels that can be attached to the symbol.
    if len(cleaned) > 10 and cleaned.startswith('CRYPTO'):
        cleaned = cleaned[6:]
    if len(cleaned) == 6:
        base, quote = cleaned[:3], cleaned[3:]
        if base in CURRENCIES and quote in CURRENCIES:
            return cleaned + ('-OTC' if otc else ''), ('otc_forex' if otc else 'normal_forex'), otc
    # Crypto markets are commonly BASEQUOTE, including USD/USDT/BTC quote forms.
    quote_codes = ('USDT', 'USD', 'BTC', 'ETH', 'EUR', 'BRL', 'GBP', 'JPY', 'TRY')
    for quote in sorted(quote_codes, key=len, reverse=True):
        if cleaned.endswith(quote):
            base = cleaned[:-len(quote)]
            if base in CRYPTO_CODES and base != quote:
                pair = base + quote
                return pair + ('-OTC' if otc else ''), ('otc_crypto' if otc else 'crypto'), otc
    # Some crypto symbols are shown as a six-character pair, e.g. BTCUSD.
    if len(cleaned) == 6 and cleaned[:3] in CRYPTO_CODES and cleaned[3:] in CURRENCIES:
        return cleaned + ('-OTC' if otc else ''), ('otc_crypto' if otc else 'crypto'), otc
    return None, None, otc


def add_asset(found, name, active_id=None):
    symbol, kind, otc = classify_asset_name(name)
    if not symbol:
        return
    raw = str(name).strip().upper().split('.')[-1].strip()
    raw_clean = re.sub(r'[^A-Z0-9]', '', raw)
    suffix = '-OTC' if otc else ''
    # Use the canonical pair name, but keep OTC suffix to find broker IDs.
    item_name = symbol
    active_map = getattr(OP_code, 'ACTIVES', {})
    resolved = None
    for candidate in (
        active_map.get(raw), active_map.get(item_name), active_map.get(symbol),
        active_map.get(str(name).strip()), active_id
    ):
        try:
            if candidate is not None:
                resolved = int(candidate)
                break
        except (TypeError, ValueError, OverflowError):
            pass
    found[item_name] = {
        'name': item_name, 'pair': symbol, 'otc': otc,
        'market_type': kind, 'id': resolved
    }
    if resolved is not None:
        try:
            OP_code.ACTIVES[item_name] = resolved
            OP_code.ACTIVES[symbol] = resolved
        except Exception:
            pass


def walk_asset_tree(node, found, parent=None):
    if isinstance(node, dict):
        name = node.get('name') or node.get('active_name') or node.get('symbol')
        active_id = node.get('id') or node.get('active_id') or node.get('activeId')
        if name:
            add_asset(found, str(name), active_id if active_id is not None else parent)
        elif isinstance(parent, str):
            add_asset(found, parent, active_id)
        for key, value in node.items():
            walk_asset_tree(value, found, key)
    elif isinstance(node, list):
        for item in node:
            walk_asset_tree(item, found, parent)


def refresh_assets(force=False):
    global ASSETS, ASSET_REFRESH_AT
    if ASSETS and not force and time.time() - ASSET_REFRESH_AT < ASSET_REFRESH_SECONDS:
        return ASSETS
    found = {}
    try:
        active_map = IQ.get_all_ACTIVES_OPCODE()
        if isinstance(active_map, dict):
            for name, active_id in active_map.items():
                add_asset(found, str(name), active_id)
    except Exception as exc:
        logging.warning('Active-code discovery failed: %s', exc)
    for method_name in ('get_all_init_v2', 'get_all_init'):
        try:
            method = getattr(IQ, method_name, None)
            if callable(method):
                walk_asset_tree(method(), found)
        except Exception as exc:
            logging.warning('%s discovery failed: %s', method_name, exc)
    ASSETS = sorted(found.values(), key=lambda item: (item.get('market_type', ''), item['name']))
    ASSET_REFRESH_AT = time.time()
    counts = {}
    for item in ASSETS:
        kind = item.get('market_type', 'unknown')
        counts[kind] = counts.get(kind, 0) + 1
    logging.info(
        'MARKETS LOADED | normal forex=%s OTC forex=%s crypto=%s OTC crypto=%s total=%s',
        counts.get('normal_forex', 0), counts.get('otc_forex', 0),
        counts.get('crypto', 0), counts.get('otc_crypto', 0), len(ASSETS)
    )
    return ASSETS


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError('Missing IQ_EMAIL or IQ_PASSWORD secret')
    last_error = 'unknown error'
    for attempt in range(1, 6):
        try:
            client = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            result = client.connect()
            connected = result[0] if isinstance(result, tuple) else bool(result)
            if not connected:
                raise ConnectionError(str(result))
            client.change_balance(BALANCE_MODE)
            time.sleep(1)
            if not client.check_connect():
                raise ConnectionError('Connection dropped after login')
            IQ = client
            logging.info('Connected to IQ Option in PRACTICE mode')
            send_telegram(
                '<b>Scanner connected</b>\nPRACTICE only\n'
                'Forex + crypto pairs where available\nStake: ${:.2f}\n{}'.format(STAKE, format_stats())
            )
            return
        except Exception as exc:
            last_error = str(exc)
            logging.warning('Connect attempt %s/5 failed: %s', attempt, last_error)
            if attempt < 5:
                time.sleep(RECONNECT_SECONDS)
    raise RuntimeError('Could not connect: ' + last_error)


def ensure_connection():
    global IQ
    try:
        if IQ is not None and IQ.check_connect():
            return True
    except Exception:
        pass
    logging.warning('IQ connection unavailable; creating a fresh client')
    try:
        connect_iq()
        refresh_assets(True)
        return True
    except Exception as exc:
        logging.warning('Reconnect failed: %s', exc)
        time.sleep(RECONNECT_SECONDS)
        return False


def safe_get_candles(self, active, interval, count, endtime):
    try:
        active_id = getattr(OP_code, 'ACTIVES', {}).get(active)
        if active_id is None:
            return None
        self.api.candles.candles_data = None
        self.api.getcandles(active_id, interval, count, endtime)
        deadline = time.time() + CANDLE_REQUEST_TIMEOUT
        while time.time() < deadline:
            data = getattr(self.api.candles, 'candles_data', None)
            if isinstance(data, list):
                return data
            if not self.check_connect():
                return None
            time.sleep(0.1)
    except Exception as exc:
        logging.warning('Candle request error %s: %s', active, exc)
    return None


def disable_digital_open_worker(self):
    logging.info('Disabled unused digital watcher')


try:
    if hasattr(IQ_Option, '_IQ_Option__get_digital_open'):
        IQ_Option._IQ_Option__get_digital_open = disable_digital_open_worker
    IQ_Option.get_candles = safe_get_candles
except Exception as exc:
    logging.warning('Candle patch failed: %s', exc)


def momentum_series(closes, period):
    values = []
    for index in range(period, len(closes)):
        old = closes[index - period]
        values.append(0.0 if old <= 0 else (closes[index] / old - 1) * 100.0)
    return values


def percentile_value(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def get_closed_candles(asset):
    try:
        raw = IQ.get_candles(asset, CANDLE_SECONDS, CANDLE_REQUEST_COUNT, int(time.time()))
    except Exception as exc:
        inc('empty_candles')
        diag_notice(asset, 'request failed: ' + str(exc))
        return []
    if not isinstance(raw, list):
        inc('empty_candles')
        diag_notice(asset, 'API returned non-list; reconnect will be checked')
        return []
    try:
        raw.sort(key=lambda candle: int(candle.get('from', 0)))
        now = int(time.time())
        closed = [
            candle for candle in raw
            if int(candle.get('from', 0)) > 0
            and int(candle.get('from', 0)) + CANDLE_SECONDS <= now
        ]
    except Exception as exc:
        inc('empty_candles')
        diag_notice(asset, 'bad candle data: ' + str(exc))
        return []
    if not closed:
        inc('empty_candles')
        diag_notice(asset, 'zero fully closed candles')
        return []
    inc('candle_data_ok')
    need = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5
    if len(closed) < need:
        inc('insufficient_candles')
        diag_notice(asset, '{} closed candles; need {}'.format(len(closed), need))
    return closed


def candle_values(candle):
    try:
        return (
            float(candle['open']),
            float(candle.get('max', candle.get('high'))),
            float(candle.get('min', candle.get('low'))),
            float(candle['close'])
        )
    except (KeyError, TypeError, ValueError):
        return None


def bull_engulf(a, b):
    x, y = candle_values(a), candle_values(b)
    return bool(x and y and x[3] < x[0] and y[3] > y[0] and y[0] <= x[3] and y[3] >= x[0])


def bear_engulf(a, b):
    x, y = candle_values(a), candle_values(b)
    return bool(x and y and x[3] > x[0] and y[3] < y[0] and y[0] >= x[3] and y[3] <= x[0])


def hammer(candle, inverted=False):
    values = candle_values(candle)
    if not values:
        return False
    open_price, high, low, close = values
    span = high - low
    if span <= 0:
        return False
    body = max(abs(close - open_price), span * 0.03)
    upper = high - max(open_price, close)
    lower = min(open_price, close) - low
    if inverted:
        return upper >= body * 2 and lower <= body * 0.8 and min(open_price, close) <= low + span * 0.45
    return lower >= body * 2 and upper <= body * 0.8 and max(open_price, close) >= low + span * 0.55


def morning_star(a, b, c, bear=False):
    x, y, z = candle_values(a), candle_values(b), candle_values(c)
    if not x or not y or not z:
        return False
    body1 = abs(x[3] - x[0])
    body2 = abs(y[3] - y[0])
    body3 = abs(z[3] - z[0])
    midpoint = (x[0] + x[3]) / 2
    if bear:
        return x[3] > x[0] and body2 <= body1 * 0.6 and z[3] < z[0] and z[3] <= midpoint and body3 >= body1 * 0.35
    return x[3] < x[0] and body2 <= body1 * 0.6 and z[3] > z[0] and z[3] >= midpoint and body3 >= body1 * 0.35


def piercing(a, b):
    x, y = candle_values(a), candle_values(b)
    return bool(x and y and x[3] < x[0] and y[3] > y[0] and y[0] <= x[3] and y[3] > (x[0] + x[3]) / 2 and y[3] < x[0])


def dark_cloud(a, b):
    x, y = candle_values(a), candle_values(b)
    return bool(x and y and x[3] > x[0] and y[3] < y[0] and y[0] >= x[3] and y[3] < (x[0] + x[3]) / 2 and y[3] > x[0])


def three_soldiers(candles, bear=False):
    values = [candle_values(candle) for candle in candles[-3:]]
    if len(values) != 3 or any(not value for value in values):
        return False
    if bear:
        return all(value[3] < value[0] for value in values) and values[1][3] < values[0][3] and values[2][3] < values[1][3]
    return all(value[3] > value[0] for value in values) and values[1][3] > values[0][3] and values[2][3] > values[1][3]


def detect_pattern(candles):
    # Pattern is on candles[-2]; candles[-1] is a separate fully closed follow-up.
    if len(candles) < 5:
        return None
    first, middle, pattern_candle = candles[-4], candles[-3], candles[-2]
    prior = candles[-3]
    current_values = candle_values(pattern_candle)
    reference_values = candle_values(candles[-5])
    names = []
    if bull_engulf(prior, pattern_candle):
        names.append(('call', 'Bullish Engulfing'))
    if bear_engulf(prior, pattern_candle):
        names.append(('put', 'Bearish Engulfing'))
    up = bool(current_values and reference_values and current_values[3] > reference_values[3])
    down = bool(current_values and reference_values and current_values[3] < reference_values[3])
    if hammer(pattern_candle) and down:
        names.append(('call', 'Hammer'))
    if hammer(pattern_candle, True) and down:
        names.append(('call', 'Inverted Hammer'))
    if hammer(pattern_candle, True) and up:
        names.append(('put', 'Shooting Star'))
    if hammer(pattern_candle) and up:
        names.append(('put', 'Hanging Man'))
    if morning_star(first, middle, pattern_candle):
        names.append(('call', 'Morning Star'))
    if morning_star(first, middle, pattern_candle, True):
        names.append(('put', 'Evening Star'))
    if piercing(prior, pattern_candle):
        names.append(('call', 'Piercing Line'))
    if dark_cloud(prior, pattern_candle):
        names.append(('put', 'Dark Cloud Cover'))
    if three_soldiers(candles[-4:-1]):
        names.append(('call', 'Three White Soldiers'))
    if three_soldiers(candles[-4:-1], True):
        names.append(('put', 'Three Black Crows'))
    if not names:
        return None
    inc('patterns_found')
    follow = candle_values(candles[-1])
    if not follow or not current_values:
        inc('followup_rejected')
        return None
    for direction, name in names:
        if direction == 'call' and follow[3] > follow[0] and follow[3] > current_values[3]:
            inc('followup_confirmed')
            return {'direction': direction, 'pattern': name}
        if direction == 'put' and follow[3] < follow[0] and follow[3] < current_values[3]:
            inc('followup_confirmed')
            return {'direction': direction, 'pattern': name}
    inc('followup_rejected')
    return None


def evaluate_signal(candles):
    if len(candles) < MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5:
        return None
    pattern = detect_pattern(candles)
    if not pattern:
        return None
    try:
        # Last candle is the separate closed follow-up candle.
        # Momentum's current value therefore corresponds to candles[-2].
        closes = [float(candle['close']) for candle in candles[:-1]]
        candle_time = int(candles[-2].get('from', 0))
    except (KeyError, TypeError, ValueError):
        return None
    m10 = momentum_series(closes, MOMENTUM_PERIOD)
    if len(m10) < MOMENTUM_LOOKBACK + 3:
        return None

    # Confirm the move away from an extreme: previous value is the potential
    # turning point; current value must move in the intended direction.
    current, previous, before = m10[-1], m10[-2], m10[-3]
    history = m10[-(MOMENTUM_LOOKBACK + 2):-2]
    low = percentile_value(history, EXTREME_PERCENTILE)
    high = percentile_value(history, 1 - EXTREME_PERCENTILE)
    direction = pattern['direction']
    if direction == 'call':
        extreme = previous <= low
        turning = current > previous and previous <= before
    else:
        extreme = previous >= high
        turning = current < previous and previous >= before

    if extreme:
        inc('m10_extreme_pass')
    else:
        inc('m10_extreme_rejected')
        return None

    turn_distance = abs(current - previous)
    turn = turning and turn_distance >= MIN_TURN_DISTANCE
    if turn:
        inc('m10_turn_pass')
        logging.info(
            'M10 TURN PASSED | direction=%s | current=%.5f | previous_extreme=%.5f | before=%.5f | distance=%.5f',
            direction, current, previous, before, turn_distance
        )
    else:
        inc('m10_turn_rejected')
        logging.info(
            'M10 TURN REJECTED | direction=%s | current=%.5f | previous_extreme=%.5f | before=%.5f | distance=%.5f | required=%.5f | correct_turn=%s',
            direction, current, previous, before, turn_distance, MIN_TURN_DISTANCE, turning
        )
        return None

    return {
        'direction': direction, 'momentum': round(current, 5),
        'candle_time': candle_time, 'pattern': pattern['pattern'],
        'confirmation': 'Momentum 10 extreme-turn + {} + next fully closed candle'.format(pattern['pattern'])
    }


def to_float(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def betinfo_net_profit(response, order_id):
    payload = response
    if isinstance(response, tuple):
        if len(response) < 2 or response[0] is not True:
            return None
        payload = response[1]
    if not isinstance(payload, dict):
        return None
    root = payload.get('result', payload)
    data = root.get('data', {}) if isinstance(root, dict) else {}
    row = (data.get(str(order_id)) or data.get(order_id)) if isinstance(data, dict) else None
    if row is None and isinstance(root, dict):
        row = root.get(str(order_id)) or root.get(order_id)
    if not isinstance(row, dict):
        return None
    gross = to_float(row.get('profit'))
    deposit = to_float(row.get('deposit'))
    if gross is not None and deposit is not None and row.get('win') not in ('', None, 'pending', 'open'):
        return round(gross - deposit, 8)
    for key in ('profit_amount', 'profitAmount', 'close_profit', 'closeProfit', 'net_profit', 'netProfit'):
        amount = to_float(row.get(key))
        if amount is not None:
            return amount
    return None


def _betinfo_worker(order_id, event, box):
    try:
        info_method = getattr(IQ, 'get_betinfo', None)
        box['value'] = betinfo_net_profit(info_method(order_id), order_id) if callable(info_method) else None
    except Exception as exc:
        box['error'] = str(exc)
        box['value'] = None
    finally:
        event.set()


def query_order_profit(order_id):
    async_method = getattr(IQ, 'get_async_order', None)
    if callable(async_method):
        try:
            async_data = async_method(order_id)
            if isinstance(async_data, dict):
                closed = async_data.get('option-closed') or async_data.get('option_closed')
                if isinstance(closed, dict) and closed:
                    message = closed.get('msg', closed)
                    if isinstance(message, dict):
                        gross = to_float(message.get('profit_amount'))
                        amount = to_float(message.get('amount'))
                        if gross is not None and amount is not None:
                            return round(gross - amount, 8)
        except Exception as exc:
            logging.debug('get_async_order failed for %s: %s', order_id, exc)
    key = str(order_id)
    task = BETINFO_TASKS.get(key)
    if task is not None:
        event, box = task
        if event.is_set():
            BETINFO_TASKS.pop(key, None)
            value = box.get('value')
            if value is not None:
                return value
        else:
            return None
    if time.time() - BETINFO_LAST_START.get(key, 0) < 15:
        return None
    event = threading.Event()
    box = {}
    BETINFO_TASKS[key] = (event, box)
    BETINFO_LAST_START[key] = time.time()
    threading.Thread(
        target=_betinfo_worker, args=(order_id, event, box),
        daemon=True
    ).start()
    logging.info('RESULT CHECK STARTED | order_id=%s | source=get_betinfo', order_id)
    return None


def settle_result(asset, direction, broker_profit, m10=None, order_id=''):
    if broker_profit is None:
        raise ValueError('Cannot settle without numeric broker result')
    order_key = str(order_id)
    with STATE_LOCK:
        if order_key and order_key in [str(value) for value in STATE.get('settled_order_ids', [])]:
            logging.warning('DUPLICATE SETTLEMENT BLOCKED | order_id=%s', order_key)
            return
        # Existing accounting convention: a broker-reported zero is counted
        # as a full $1 stake loss, matching the user's requested statistics.
        net = -STAKE if float(broker_profit) == 0 else float(broker_profit)
        win = net > 0
        STATE['profit'] = round(float(STATE.get('profit', 0)) + net, 2)
        STATE['resolved'] = int(STATE.get('resolved', 0)) + 1
        if win:
            STATE['wins'] = int(STATE.get('wins', 0)) + 1
            STATE['consecutive_losses'] = 0
            label = 'WIN'
        else:
            STATE['losses'] = int(STATE.get('losses', 0)) + 1
            STATE['consecutive_losses'] = int(STATE.get('consecutive_losses', 0)) + 1
            label = 'LOSS'
        if order_key:
            STATE.setdefault('settled_order_ids', []).append(order_key)
        if str(STATE.get('pending_order_id')) == order_key:
            for key in ('pending_order_id', 'pending_asset', 'pending_direction', 'pending_started'):
                STATE[key] = None
    log_trade(asset, direction, m10, label, round(net, 2), order_id)
    save_state(True)
    logging.info('%s %s %s %+.2f | %s', label, asset, direction, net, format_stats())
    send_telegram(
        '<b>Trade result: {}</b>\n{} {}\nBroker: ${:+.2f}\nAccounted: ${:+.2f}\n{}'.format(
            label, asset, direction.upper(), broker_profit, net, format_stats()
        )
    )
    stop_if_limit_reached()


def wait_result(order_id, asset, direction, m10):
    logging.info(
        'RESULT MONITOR ACTIVE | order_id=%s | asset=%s | expiry=%sm',
        order_id, asset, EXPIRY_MINUTES
    )
    last_notice = 0
    while not STOP_EVENT.is_set():
        if not ensure_connection():
            time.sleep(ORDER_RESULT_RETRY_SECONDS)
            continue
        value = query_order_profit(order_id)
        if value is not None:
            settle_result(asset, direction, value, m10, order_id)
            return
        if time.time() - last_notice >= 60:
            logging.warning('Broker result unreadable for order %s; keeping pending', order_id)
            last_notice = time.time()
        time.sleep(ORDER_RESULT_RETRY_SECONDS)


def start_result_monitor(order_id, asset, direction, m10=None):
    key = str(order_id)
    with RESULT_MONITOR_LOCK:
        current = RESULT_MONITORS.get(key)
        if current is not None and current.is_alive():
            logging.info('RESULT MONITOR ALREADY ACTIVE | order_id=%s', order_id)
            return
        thread = threading.Thread(
            target=wait_result, args=(order_id, asset, direction, m10),
            name='result-monitor-' + key, daemon=True
        )
        RESULT_MONITORS[key] = thread
        thread.start()
    logging.info('RESULT MONITOR STARTED IN BACKGROUND | order_id=%s', order_id)


def recover_pending_order():
    order_id = STATE.get('pending_order_id')
    if order_id:
        asset = STATE.get('pending_asset') or 'UNKNOWN'
        direction = STATE.get('pending_direction') or 'unknown'
        logging.warning(
            'Recovering pending order in background | order_id=%s | asset=%s',
            order_id, asset
        )
        start_result_monitor(order_id, asset, direction, None)


def place_trade(item, signal):
    asset = item['name']
    direction = signal['direction']
    with TRADE_LOCK:
        if STOP_EVENT.is_set() or stop_if_limit_reached() or STATE.get('pending_order_id'):
            logging.warning('ORDER SKIPPED | %s %s | stopped or another order is pending', asset, direction)
            return
        active_id = item.get('id')
        if active_id is not None:
            try:
                OP_code.ACTIVES[asset] = int(active_id)
            except Exception:
                pass
        if asset not in getattr(OP_code, 'ACTIVES', {}):
            inc('order_rejected')
            log_trade(asset, direction, signal['momentum'], 'ORDER_REJECTED_NO_ACTIVE_ID')
            logging.error('ORDER REJECTED | missing active ID for %s', asset)
            REJECTED_ASSETS[asset] = time.time() + REJECTED_ASSET_COOLDOWN_SECONDS
            return
        if not ensure_connection():
            logging.warning('ORDER NOT SENT | connection unavailable for %s', asset)
            return
        logging.info(
            'ORDER SUBMIT BEGIN | PRACTICE | asset=%s | direction=%s | stake=$%.2f | expiry=%sm',
            asset, direction.upper(), STAKE, EXPIRY_MINUTES
        )
        try:
            result = IQ.buy(STAKE, asset, direction, EXPIRY_MINUTES)
        except Exception as exc:
            log_trade(asset, direction, signal['momentum'], 'ORDER_ERROR')
            logging.exception('ORDER SUBMIT EXCEPTION | %s | %s', asset, exc)
            send_telegram(
                '<b>Order submission error</b>\n{} {}\nNo automatic retry was made.'.format(
                    asset, direction.upper()
                )
            )
            return
        logging.info('ORDER SUBMIT RESPONSE | asset=%s | response=%r', asset, result)
        success = False
        order_id = None
        reason = None
        if isinstance(result, (tuple, list)):
            success = bool(result[0]) if result else False
            order_id = result[1] if len(result) > 1 else None
            if not success:
                reason = order_id
        else:
            success = bool(result)
            order_id = result if success else None
        if not success or order_id is None or order_id is False:
            REJECTED_ASSETS[asset] = time.time() + REJECTED_ASSET_COOLDOWN_SECONDS
            inc('order_rejected')
            log_trade(asset, direction, signal['momentum'], 'ORDER_REJECTED')
            logging.error(
                'ORDER REJECTED | asset=%s | response=%r | reason=%r',
                asset, result, reason
            )
            return
        with STATE_LOCK:
            STATE['pending_order_id'] = order_id
            STATE['pending_asset'] = asset
            STATE['pending_direction'] = direction
            STATE['pending_started'] = now_lagos().isoformat(timespec='seconds')
        save_state(True)
        log_trade(asset, direction, signal['momentum'], 'OPENED', '', order_id)
        logging.info(
            'ORDER OPENED | asset=%s | direction=%s | order_id=%s',
            asset, direction.upper(), order_id
        )
        send_telegram(
            '<b>Practice trade opened</b>\n{} {}\nOrder ID: {}\nStake: ${:.2f}\nM10: {:.5f}\n{}'.format(
                asset, direction.upper(), order_id, STAKE, signal['momentum'], signal['confirmation']
            )
        )
    start_result_monitor(order_id, asset, direction, signal['momentum'])


def report_diagnostics():
    with DIAG_LOCK:
        values = dict(DIAG)
        for key in DIAG:
            DIAG[key] = 0
    logging.info(
        'SCAN DIAGNOSTICS | scanned=%s candle_ok=%s empty=%s insufficient=%s evaluated=%s patterns=%s followup_ok=%s followup_rejected=%s m10_extreme_ok=%s m10_extreme_rejected=%s m10_turn_ok=%s m10_turn_rejected=%s setups=%s duplicates=%s order_rejected=%s',
        *[values[key] for key in DIAG_KEYS]
    )


def reconcile_known_closed_order():
    # Historical Cosmos OTC order confirmed closed in IQ Option history.
    # Clear it without charging a previous-day loss to today's risk totals.
    known_order_id = '14383554604'
    if str(STATE.get('pending_order_id')) != known_order_id:
        return False
    if known_order_id not in [str(value) for value in STATE.get('settled_order_ids', [])]:
        log_trade(
            STATE.get('pending_asset') or 'ATOMUSD-OTC',
            STATE.get('pending_direction') or 'put', None,
            'HISTORICAL_CONFIRMED_LOSS', -1.50, known_order_id
        )
        STATE.setdefault('settled_order_ids', []).append(known_order_id)
        logging.warning(
            'RECONCILED HISTORICAL ORDER | id=%s | net=-$1.50 | not added to current-day risk totals',
            known_order_id
        )
    for key in ('pending_order_id', 'pending_asset', 'pending_direction', 'pending_started'):
        STATE[key] = None
    save_state(True)
    send_telegram(
        '<b>Historical order reconciled</b>\nOrder ID: {}\nConfirmed net result: -$1.50\n'
        'Pending lock cleared; current-day totals unchanged.'.format(known_order_id)
    )
    return True


def main():
    connect_iq()
    reconcile_known_closed_order()
    refresh_assets(True)
    if not ASSETS:
        raise RuntimeError('No supported forex or crypto assets discovered')
    if stop_if_limit_reached():
        return
    if STATE.get('pending_order_id'):
        recover_pending_order()
        logging.warning('Previous order unresolved; new orders remain blocked until result is confirmed.')
    send_telegram(
        '<b>Scanner started</b>\nPRACTICE only\nForex + crypto pairs where available; '
        'Momentum 10 extreme-turn; 12 patterns + separate closed confirmation candle\n{}'.format(
            format_stats()
        )
    )
    heartbeat = time.time()
    diagnostic = time.time()
    while not STOP_EVENT.is_set():
        if stop_if_limit_reached():
            break
        if not ensure_connection():
            continue
        if time.time() - ASSET_REFRESH_AT >= ASSET_REFRESH_SECONDS:
            refresh_assets(True)
        if time.time() - heartbeat >= HEARTBEAT_SECONDS:
            logging.info(
                'HEARTBEAT | %s | assets=%s | %s',
                now_lagos().strftime('%Y-%m-%d %H:%M:%S WAT'), len(ASSETS), format_stats()
            )
            send_telegram(
                '<b>Scanner heartbeat</b>\n{}\n{}'.format(
                    now_lagos().strftime('%Y-%m-%d %H:%M:%S WAT'), format_stats()
                )
            )
            heartbeat = time.time()
        if time.time() - diagnostic >= DIAGNOSTIC_LOG_SECONDS:
            report_diagnostics()
            diagnostic = time.time()
        for item in list(ASSETS):
            if STOP_EVENT.is_set() or stop_if_limit_reached():
                break
            asset = item['name']
            try:
                inc('assets_scanned')
                cooldown = REJECTED_ASSETS.get(asset, 0)
                if time.time() < cooldown:
                    continue
                if cooldown:
                    REJECTED_ASSETS.pop(asset, None)
                candles = get_closed_candles(asset)
                if len(candles) < MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5:
                    if not ensure_connection():
                        break
                    continue
                inc('signals_checked')
                signal = evaluate_signal(candles)
                if not signal:
                    continue
                inc('setups_found')
                signal_key = str(signal['candle_time'])
                last_signals = STATE.setdefault('last_signals', {})
                if last_signals.get(asset) == signal_key:
                    inc('duplicate_signals')
                    continue
                last_signals[asset] = signal_key
                save_state()
                log_trade(asset, signal['direction'], signal['momentum'], 'SIGNAL')
                logging.info(
                    'CONFIRMED %s [%s] %s M10=%.5f pattern=%s',
                    asset, item.get('market_type', 'unknown'), signal['direction'],
                    signal['momentum'], signal['pattern']
                )
                send_telegram(
                    '<b>Confirmed setup</b>\n{} {}\nMarket: {}\nPattern: {}\nM10: {:.5f}\n{}'.format(
                        asset, signal['direction'].upper(), item.get('market_type', 'unknown'),
                        signal['pattern'], signal['momentum'], signal['confirmation']
                    )
                )
                if AUTO_TRADE:
                    place_trade(item, signal)
            except Exception as exc:
                logging.warning('Scan error for %s: %s', asset, exc)
            time.sleep(0.15)
        time.sleep(SCAN_INTERVAL)
    reason = STATE.get('stop_reason') or risk_stop_reason() or 'Scanner stopped'
    logging.info('Finished: %s | %s', reason, format_stats())
    send_telegram('<b>Scanner finished</b>\n{}\n{}'.format(reason, format_stats()))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        save_state(True)
        logging.info('Stopped by keyboard interrupt')
    except Exception as exc:
        logging.exception('Fatal scanner error')
        send_telegram('<b>Scanner error</b>\n{}'.format(exc))
        save_state(True)
        raise
