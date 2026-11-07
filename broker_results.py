"""Bounded, refreshable result lookup for IQ Option binary/turbo orders.

The broker's async order cache is checked continuously. Closed-option history
is refreshed periodically because one early history snapshot can arrive before
the just-expired order has been indexed by the broker.
"""
import math
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
        if str(option.get("win", "")).lower() in ("equal", "draw"):
            return 0.0
        try:
            profit = float(option["win_amount"])
            amount = float(option["amount"])
            if not math.isfinite(profit) or not math.isfinite(amount):
                return None
            return round(profit - amount, 2)
        except (KeyError, TypeError, ValueError, OverflowError):
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
        if not math.isfinite(profit_amount) or not math.isfinite(amount):
            return None
        return round(profit_amount - amount, 2)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _closed_options_from_payload(payload):
    if not isinstance(payload, dict):
        return []
    msg = payload.get("msg")
    if not isinstance(msg, dict):
        return []
    rows = msg.get("closed_options", [])
    return rows if isinstance(rows, list) else []


def get_binary_option_net_profit(client, order_id, lock=None, timeout=35, poll_interval=0.2):
    """Return net P/L only after an order-specific broker result is confirmed.

    Both broker sources are polled until the deadline:
    1. The order-specific asynchronous option-closed cache.
    2. Fresh closed_options history snapshots, requested repeatedly.

    A missing result remains an exception; it is never guessed as a loss.
    """
    api = getattr(client, "api", None)
    if api is None or not hasattr(api, "get_options_v2") or not hasattr(api, "get_options_v2_data"):
        raise RuntimeError("IQ Option client does not expose binary closed-option history")

    try:
        timeout = max(0.1, float(timeout))
    except (TypeError, ValueError, OverflowError):
        timeout = 35.0
    try:
        poll_interval = max(0.05, float(poll_interval))
    except (TypeError, ValueError, OverflowError):
        poll_interval = 0.2

    deadline = time.monotonic() + timeout
    if not _RESULT_LOOKUP_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("Another broker-result lookup is still running")

    try:
        async_lookup = getattr(client, "get_async_order", None)
        lookup_id = int(order_id) if str(order_id).isdigit() else order_id
        history_refresh_interval = max(0.1, min(1.0, poll_interval * 5.0))
        next_history_refresh = 0.0
        last_history_error = None

        while time.monotonic() < deadline:
            # Check the order-specific cache on every pass. The cache is
            # populated by websocket events and does not require a new request.
            if callable(async_lookup):
                try:
                    pnl = extract_async_order_net_profit(async_lookup(lookup_id))
                    if pnl is not None:
                        return pnl
                except Exception:
                    pass

            now = time.monotonic()
            if now >= next_history_refresh:
                acquired = False
                try:
                    if lock is not None:
                        remaining = max(0.0, deadline - time.monotonic())
                        acquired = lock.acquire(timeout=min(1.0, remaining))
                        if not acquired:
                            next_history_refresh = time.monotonic() + min(0.25, poll_interval)
                            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
                            continue

                    # Clear the previous response before every request. This
                    # prevents repeatedly inspecting one stale history snapshot.
                    api.get_options_v2_data = None
                    api.get_options_v2(100, "binary,turbo")
                    last_history_error = None
                    next_history_refresh = time.monotonic() + history_refresh_interval
                except Exception as exc:
                    last_history_error = exc
                    next_history_refresh = time.monotonic() + history_refresh_interval
                finally:
                    if acquired:
                        lock.release()

            # A new history response may arrive asynchronously after the
            # request. Inspect it every pass, then request another snapshot
            # after the refresh interval if this one does not contain the ID.
            payload = getattr(api, "get_options_v2_data", None)
            pnl = extract_net_profit(_closed_options_from_payload(payload), order_id)
            if pnl is not None:
                return pnl

            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

        detail = f"; latest history request error: {last_history_error}" if last_history_error else ""
        raise TimeoutError(
            "Neither order-specific async result nor refreshed closed-option history "
            f"confirmed order {order_id} before timeout{detail}"
        )
    finally:
        _RESULT_LOOKUP_LOCK.release()
