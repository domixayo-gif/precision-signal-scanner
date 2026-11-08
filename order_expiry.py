"""Helpers for explicit IQ Option binary/turbo expiry timestamps."""
import math


def calculate_raw_expiry(server_timestamp, expiry_minutes):
    """Build an absolute expiry from the broker's synchronized server clock.

    The timestamp is sent before broker acceptance, so network/acknowledgement
    latency prevents a mathematical guarantee of 120 seconds after acceptance.
    This avoids the library's minute-boundary duration calculation.
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
    return math.ceil(server_time) + minutes * 60
