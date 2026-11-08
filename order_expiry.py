"""Helpers for IQ Option's broker-supported option expiry timestamps."""
import math


def calculate_raw_expiry(server_timestamp, expiry_minutes):
    """Return the nearest supported expiry timestamp for an option duration.

    IQ Option turbo/binary options do not accept arbitrary server_time + N
    seconds expiries. They expire on supported minute/quarter-hour boundaries,
    and the next minute is unavailable when it is 30 seconds or less away.
    This mirrors the candidate selection used by iqoptionapi.expiration.
    """
    try:
        server_time = float(server_timestamp)
        minutes = int(expiry_minutes)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Broker server time and expiry duration are required")
    if not math.isfinite(server_time) or server_time <= 0:
        raise ValueError("Broker server time is unavailable or invalid")
    if minutes <= 0:
        raise ValueError("Expiry duration must be positive")

    now = int(server_time)
    minute_floor = now - (now % 60)
    next_minute = minute_floor + 60
    first_expiry = next_minute if (next_minute - server_time) > 30 else next_minute + 60

    candidates = [first_expiry + 60 * offset for offset in range(5)]

    # Longer binary expiries are aligned to quarter-hour marks, at least
    # five minutes ahead, matching the upstream library's candidate list.
    quarter = ((now // 900) + 1) * 900
    added = 0
    while added < 50:
        if quarter - server_time > 300:
            candidates.append(quarter)
            added += 1
        quarter += 900

    target_seconds = minutes * 60
    return int(min(candidates, key=lambda expiry: abs((expiry - server_time) - target_seconds)))
