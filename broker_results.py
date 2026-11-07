"""Bounded result lookup for IQ Option binary/turbo orders.

The installed community API's check_win_v3() polls closed_options but has no
timeout. This helper performs the same lookup with a bounded wait and refreshes
history until the requested order appears or the deadline expires.
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


def get_binary_option_net_profit(
    client, order_id, lock=None, timeout=35, poll_interval=0.2,
    history_refresh_interval=1.0,
):
    """Return confirmed net P/L, refreshing stale history until timeout.

    Both result sources are checked throughout the same bounded window. A
    history response that does not contain this order is not treated as final:
    a fresh closed-options request is issued before the deadline.
    """
    api = getattr(client, "api", None)
    if api is None or not hasattr(api, "get_options_v2") or not hasattr(api, "get_options_v2_data"):
        raise RuntimeError("IQ Option client does not expose binary closed-option history")
    if not _RESULT_LOOKUP_LOCK.acquire(timeout=timeout):
        raise TimeoutError("Another broker-result lookup is still running")
    try:
        async_lookup = getattr(client, "get_async_order", None)
        lookup_id = int(order_id) if str(order_id).isdigit() else order_id
        deadline = time.monotonic() + timeout
        next_history_refresh = 0.0
        history_response_deadline = 0.0
        history_request_pending = False
        last_history_payload = None

        while time.monotonic() < deadline:
            if callable(async_lookup):
                try:
                    pnl = extract_async_order_net_profit(async_lookup(lookup_id))
                    if pnl is not None:
                        return pnl
                except Exception:
                    # The async cache may not have received the close event yet.
                    pass

            now = time.monotonic()
            if history_request_pending and now >= history_response_deadline:
                # A request that produced no response must not block later retries.
                history_request_pending = False
                next_history_refresh = now

            if not history_request_pending and now >= next_history_refresh:
                acquired = False
                try:
                    if lock is not None:
                        remaining = max(0.1, deadline - now)
                        acquired = lock.acquire(timeout=min(2.0, remaining))
                        if not acquired:
                            next_history_refresh = time.monotonic() + poll_interval
                            time.sleep(poll_interval)
                            continue
                    api.get_options_v2_data = None
                    api.get_options_v2(100, "binary,turbo")
                    history_request_pending = True
                    history_response_deadline = time.monotonic() + max(
                        2.0, poll_interval * 10
                    )
                except Exception:
                    history_request_pending = False
                    next_history_refresh = time.monotonic() + max(
                        poll_interval, history_refresh_interval
                    )
                finally:
                    if acquired:
                        lock.release()

            payload = getattr(api, "get_options_v2_data", None)
            if isinstance(payload, dict) and payload is not last_history_payload:
                last_history_payload = payload
                msg = payload.get("msg", {})
                closed = msg.get("closed_options", []) if isinstance(msg, dict) else []
                pnl = extract_net_profit(closed, order_id)
                if pnl is not None:
                    return pnl
                # This response was valid but did not yet contain our order.
                history_request_pending = False
                next_history_refresh = time.monotonic() + max(
                    poll_interval, history_refresh_interval
                )

            time.sleep(poll_interval)

        raise TimeoutError(
            "Neither order-specific async result nor refreshed closed-option history "
            "confirmed this order before timeout"
        )
    finally:
        _RESULT_LOOKUP_LOCK.release()
