"""Confirmed support/resistance breakout followed by the first valid retest.

Pure Python: no broker, network, order, or account access.
Input candles must be closed and ordered oldest -> newest.
"""
from dataclasses import dataclass
from enum import Enum
from statistics import median


class RetestState(Enum):
    NONE = "none"
    RETESTED = "retested"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class RetestResult:
    state: RetestState
    direction: str | None = None  # "up" / "down"
    level: float | None = None
    zone: tuple[float, float] | None = None
    reasons: tuple[str, ...] = ()
    retest_index: int | None = None


@dataclass(frozen=True)
class FilterConfig:
    pivot_n: int = 3
    atr_period: int = 14
    cluster_atr: float = 0.25
    min_touches: int = 2
    min_level_age: int = 15
    break_close_atr: float = 0.30
    break_body_atr: float = 0.60
    break_body_avg: float = 1.20
    max_setup_age: int = 8
    zone_atr: float = 0.25
    max_close_through_atr: float = 0.15
    max_retrace: float = 0.61
    max_pivot_range_atr: float = 3.0


DEFAULT_CONFIG = FilterConfig()


def _num(candle, key):
    value = float(candle[key])
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("non-finite candle value")
    return value


def _atr(candles, end, period):
    start = max(1, end - period + 1)
    true_ranges = []
    for i in range(start, end + 1):
        high = _num(candles[i], "high")
        low = _num(candles[i], "low")
        previous_close = _num(candles[i - 1], "close")
        true_ranges.append(max(high - low, abs(high - previous_close),
                               abs(low - previous_close)))
    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def _pivots(candles, side, n, cfg):
    found = []
    for i in range(n, len(candles) - n):
        atr = _atr(candles, i, cfg.atr_period)
        if atr <= 0:
            continue
        high = _num(candles[i], "high")
        low = _num(candles[i], "low")
        if high - low > cfg.max_pivot_range_atr * atr:
            continue
        before = candles[i - n:i]
        after = candles[i + 1:i + n + 1]
        if side == "up":
            if high >= max(_num(c, "high") for c in before + after):
                found.append((i, high))
        else:
            if low <= min(_num(c, "low") for c in before + after):
                found.append((i, low))
    return found


def _level_for_breakout(pivots, breakout_index, atr, cfg):
    eligible = [(i, price) for i, price in pivots
                if i <= breakout_index - cfg.pivot_n
                and breakout_index - i >= cfg.min_level_age]
    best = None
    for _, seed in eligible:
        cluster = [(i, price) for i, price in eligible
                   if abs(price - seed) <= cfg.cluster_atr * atr]
        if len(cluster) < cfg.min_touches:
            continue
        level = median(price for _, price in cluster)
        # Prefer the strongest cluster; ties prefer the most recently tested.
        rank = (len(cluster), max(i for i, _ in cluster))
        if best is None or rank > best[0]:
            best = (rank, level)
    return best[1] if best else None


def evaluate(candles, cfg=DEFAULT_CONFIG):
    """Detect a valid breakout followed by its first retest.

    Entry readiness is reached on the first closed candle that touches the
    broken level and closes without decisively crossing back through it.
    No second pullback candle or continuation/confirmation candle is required.
    """
    try:
        if not isinstance(candles, list) or len(candles) < max(28, cfg.min_level_age + 2 * cfg.pivot_n + 5):
            return RetestResult(RetestState.NONE, reasons=("insufficient closed candles",))

        up_pivots = _pivots(candles, "up", cfg.pivot_n, cfg)
        down_pivots = _pivots(candles, "down", cfg.pivot_n, cfg)
        latest = len(candles) - 1
        candidates = []

        first_break = max(cfg.min_level_age + 2 * cfg.pivot_n, latest - cfg.max_setup_age)
        last_break = latest - 1
        for direction, pivots in (("up", up_pivots), ("down", down_pivots)):
            for b in range(first_break, last_break + 1):
                atr = _atr(candles, b, cfg.atr_period)
                if atr <= 0:
                    continue
                level = _level_for_breakout(pivots, b, atr, cfg)
                if level is None:
                    continue

                breakout = candles[b]
                op, cl = _num(breakout, "open"), _num(breakout, "close")
                hi, lo = _num(breakout, "high"), _num(breakout, "low")
                body = abs(cl - op)
                prior_bodies = [abs(_num(c, "close") - _num(c, "open"))
                                for c in candles[max(0, b - 10):b]]
                avg_body = sum(prior_bodies) / len(prior_bodies) if prior_bodies else 0.0
                strong_body = body >= cfg.break_body_atr * atr or body >= cfg.break_body_avg * avg_body
                impulse_window = candles[max(0, b - 10):b + 1]

                if direction == "up":
                    valid_break = cl > level + cfg.break_close_atr * atr and cl > op
                    leg = hi - min(_num(c, "low") for c in impulse_window)
                else:
                    valid_break = cl < level - cfg.break_close_atr * atr and cl < op
                    leg = max(_num(c, "high") for c in impulse_window) - lo
                if not valid_break or not strong_body or leg <= 0:
                    continue

                # Search forward for the FIRST retest, not only the candle
                # immediately after breakout. The setup remains valid for at
                # most max_setup_age closed candles after the breakout.
                zone = (level - cfg.zone_atr * atr, level + cfg.zone_atr * atr)
                search_end = min(latest, b + cfg.max_setup_age)
                for retest_index in range(b + 1, search_end + 1):
                    retest = candles[retest_index]
                    retest_close = _num(retest, "close")

                    if direction == "up":
                        extreme = _num(retest, "low")
                        touched = extreme <= zone[1]
                        held = retest_close >= level - cfg.max_close_through_atr * atr
                        depth = (hi - extreme) / leg
                        decisively_broken = retest_close < level - cfg.max_close_through_atr * atr
                    else:
                        extreme = _num(retest, "high")
                        touched = extreme >= zone[0]
                        held = retest_close <= level + cfg.max_close_through_atr * atr
                        depth = (extreme - lo) / leg
                        decisively_broken = retest_close > level + cfg.max_close_through_atr * atr

                    # A decisive close through the level invalidates the setup,
                    # even if the candle did not enter the touch zone.
                    if decisively_broken:
                        candidates.append((b, RetestResult(
                            RetestState.INVALIDATED, direction, level, zone,
                            ("price closed decisively through the broken level before a valid retest",),
                            retest_index
                        )))
                        break
                    if not touched:
                        continue
                    if not held or depth < 0 or depth > cfg.max_retrace:
                        candidates.append((b, RetestResult(
                            RetestState.INVALIDATED, direction, level, zone,
                            ("the first retest exceeded the allowed hold or retracement depth",),
                            retest_index
                        )))
                        break

                    candidates.append((b, RetestResult(
                        RetestState.RETESTED, direction, level, zone,
                        ("first valid breakout retest detected; entry does not wait for confirmation candles",),
                        retest_index
                    )))
                    break

        if not candidates:
            return RetestResult(RetestState.NONE, reasons=("no valid breakout followed by a retest",))
        return max(candidates, key=lambda item: item[0])[1]
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as exc:
        return RetestResult(RetestState.NONE, reasons=(f"filter safely skipped: {type(exc).__name__}",))

def build_entry_signal(asset, candles, candle_seconds=60, cfg=DEFAULT_CONFIG):
    """Build an executable signal only when the first retest is the latest closed bar.

    The signal timestamp is the retest candle's CLOSE time, not its open time,
    so stale retests cannot be mistaken for fresh entries by the scanner.
    """
    result = evaluate(candles, cfg)
    if result.state != RetestState.RETESTED:
        return None, result
    if result.retest_index != len(candles) - 1:
        return None, result
    try:
        candle = candles[result.retest_index]
        candle_open_time = int(candle["from"])
        candle_close_time = candle_open_time + int(candle_seconds)
        signal = {
            "asset": str(asset),
            "direction": "call" if result.direction == "up" else "put",
            "score": 100,
            "candle_time": candle_close_time,
            "entry_time": candle_close_time,
            "price": _num(candle, "close"),
            "level": result.level,
            "zone": result.zone,
            "source": "Pure breakout + first valid retest",
        }
        return signal, result
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, RetestResult(
            RetestState.NONE,
            reasons=("retest candle timestamp or price is invalid",),
        )
