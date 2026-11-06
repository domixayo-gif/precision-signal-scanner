"""Bounded result lookup for IQ Option binary/turbo orders.

The installed community API's check_win_v3() polls closed_options but has no
timeout. This helper performs the same lookup with a bounded wait.
"""
import threading
import time

_RESULT_LOOKUP_LOCK = threading.Lock()


def extract_net_profit(closed_options, order_id):
    if not isinstance(closed_options, list):
        return None
    wanted = str(order_id)
    for option in closed_options:
        if not isinstance(option, dict):
            continue
        raw_id = option.get("id")
        ids = raw_id if isinstance(raw_id, (list, tuple)) else [raw_id]
        if wanted not in {str(value) for value in ids if value is not None}:
            continue
        if option.get("win") == "equal":
            return 0.0
        try:
            return round(float(option["win_amount"]) - float(option["amount"]), 2)
        except (KeyError, TypeError, ValueError):
            return None
    return None


def extract_async_order_net_profit(order_data):
    """Extract realized P/L from IQ Option's order-specific async result."""
    if not isinstance(order_data, dict):
        return None
    closed = order_data.get("option-closed")
    if not isinstance(closed, dict) or not closed:
        return None
    msg = closed.get("msg")
    if not isinstance(msg, dict):
        return None
    try:
        profit_amount = float(msg["profit_amount"])
        amount = float(msg["amount"])
        return round(profit_amount - amount, 2)
    except (KeyError, TypeError, ValueError):
        return None


def get_binary_option_net_profit(client, order_id, lock=None, timeout=35, poll_interval=0.2):
    api = getattr(client, "api", None)
    if api is None or not hasattr(api, "get_options_v2") or not hasattr(api, "get_options_v2_data"):
        raise RuntimeError("IQ Option client does not expose binary closed-option history")
    if not _RESULT_LOOKUP_LOCK.acquire(timeout=timeout):
        raise TimeoutError("Another broker-result lookup is still running")
    try:
        # Prefer the order-specific async cache while this process still owns
        # the order ID. General closed-options history can lag behind expiry.
        async_lookup = getattr(client, "get_async_order", None)
        async_deadline = time.monotonic() + timeout * 0.7
        lookup_id = int(order_id) if str(order_id).isdigit() else order_id
        if callable(async_lookup):
            while time.monotonic() < async_deadline:
                try:
                    pnl = extract_async_order_net_profit(async_lookup(lookup_id))
                    if pnl is not None:
                        return pnl
                except Exception:
                    pass
                time.sleep(poll_interval)

        acquired = False
        if lock is not None:
            acquired = lock.acquire(timeout=min(8, timeout))
            if not acquired:
                raise TimeoutError("IQ Option API busy; result lookup deferred")
        try:
            api.get_options_v2_data = None
            api.get_options_v2(100, "binary,turbo")
        finally:
            if acquired:
                lock.release()

        deadline = time.monotonic() + max(0.1, timeout * 0.3)
        while time.monotonic() < deadline:
            payload = getattr(api, "get_options_v2_data", None)
            if isinstance(payload, dict):
                msg = payload.get("msg", {})
                closed = msg.get("closed_options", []) if isinstance(msg, dict) else []
                pnl = extract_net_profit(closed, order_id)
                if pnl is not None:
                    return pnl
            time.sleep(poll_interval)
        raise TimeoutError("Neither order-specific async result nor closed-option history confirmed this order before timeout")
    finally:
        _RESULT_LOOKUP_LOCK.release()
