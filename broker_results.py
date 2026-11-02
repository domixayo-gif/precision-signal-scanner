"""Bounded result lookup for IQ Option binary/turbo orders."""
import threading
import time

_RESULT_LOOKUP_LOCK = threading.Lock()


def _find_order_payload(payload, order_id):
    if not isinstance(payload, dict):
        return None
    root = payload.get("result", payload)
    if not isinstance(root, dict):
        return None
    data = root.get("data", root)
    if not isinstance(data, dict):
        return None
    return data.get(str(order_id)) or data.get(order_id)


def extract_net_profit(payload, order_id):
    row = _find_order_payload(payload, order_id)
    if not isinstance(row, dict) or row.get("win") in (None, ""):
        return None
    try:
        return round(float(row["profit"]) - float(row["deposit"]), 2)
    except (KeyError, TypeError, ValueError):
        return None


def get_binary_option_net_profit(client, order_id, lock=None, timeout=12, poll_interval=0.2):
    api = getattr(client, "api", None)
    if api is None or not hasattr(api, "get_betinfo") or not hasattr(api, "game_betinfo"):
        raise RuntimeError("IQ Option client does not expose binary bet-info lookup")
    if not _RESULT_LOOKUP_LOCK.acquire(timeout=timeout):
        raise TimeoutError("Another broker-result lookup is still running")
    try:
        betinfo = api.game_betinfo
        acquired = False
        if lock is not None:
            acquired = lock.acquire(timeout=min(8, timeout))
            if not acquired:
                raise TimeoutError("IQ Option API busy; result lookup deferred")
        try:
            betinfo.isSuccessful = None
            api.get_betinfo(int(order_id))
        finally:
            if acquired:
                lock.release()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            successful = getattr(betinfo, "isSuccessful", None)
            if successful is True:
                pnl = extract_net_profit(getattr(betinfo, "dict", None), order_id)
                if pnl is not None:
                    return pnl
            elif successful is False:
                raise RuntimeError("IQ Option bet-info request was unsuccessful")
            time.sleep(poll_interval)
        raise TimeoutError("Broker bet-info result not available before lookup timeout")
    finally:
        _RESULT_LOOKUP_LOCK.release()
