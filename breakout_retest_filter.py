"""Two-candle breakout/retest detector for Momentum 10.

Pure Python: no broker, network, order, or account access.
Input candles must be closed and ordered oldest -> newest.
"""
from dataclasses import dataclass
from enum import Enum
from statistics import median


class RetestState(Enum):
    NONE = "none"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class RetestResult:
    state: RetestState
    direction: str | None = None  # "up" / "down"
    level: float | None = None
    zone: tuple[float, float] | None = None
    reasons: tuple[str, ...] = ()


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
    pullback_candles: int = 2
    continuation_candles: int = 2
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
    """Return the latest valid two-candle retest state.

    A bullish breakout is followed by exactly two bearish pullback candles
    that test old resistance (now support). The bearish case is mirrored.
    Two continuation candles confirm the retest. Pending retests are also
    reported so the caller can block an opposing Momentum 10 entry early.
    """
    try:
        if not isinstance(candles, list) or len(candles) < max(28, cfg.min_level_age + 2 * cfg.pivot_n + 5):
            return RetestResult(RetestState.NONE, reasons=("insufficient closed candles",))

        up_pivots = _pivots(candles, "up", cfg.pivot_n, cfg)
        down_pivots = _pivots(candles, "down", cfg.pivot_n, cfg)
        latest = len(candles) - 1
        candidates = []

        first_break = max(cfg.min_level_age + 2 * cfg.pivot_n, latest - cfg.max_setup_age)
        last_break = latest - cfg.pullback_candles
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

                if direction == "up":
                    valid_break = cl > level + cfg.break_close_atr * atr and cl > op
                    leg = hi - level
                else:
                    valid_break = cl < level - cfg.break_close_atr * atr and cl < op
                    leg = level - lo
                if not valid_break or not strong_body or leg <= 0:
                    continue

                p1, p2 = b + 1, b + 2
                if p2 > latest:
                    continue
                pullbacks = candles[p1:p2 + 1]
                o1, c1 = _num(pullbacks[0], "open"), _num(pullbacks[0], "close")
                o2, c2 = _num(pullbacks[1], "open"), _num(pullbacks[1], "close")
                if direction == "up":
                    opposite = c1 < o1 and c2 < o2
                    extreme = min(_num(c, "low") for c in pullbacks)
                    through = min(c1, c2) < level - cfg.max_close_through_atr * atr
                    touched = extreme <= level + cfg.zone_atr * atr and extreme >= level - cfg.zone_atr * atr
                    pullback_depth = (hi - extreme) / leg
                else:
                    opposite = c1 > o1 and c2 > o2
                    extreme = max(_num(c, "high") for c in pullbacks)
                    through = max(c1, c2) > level + cfg.max_close_through_atr * atr
                    touched = extreme >= level - cfg.zone_atr * atr and extreme <= level + cfg.zone_atr * atr
                    pullback_depth = (extreme - lo) / leg

                body1, body2 = abs(c1 - o1), abs(c2 - o2)
                expanding = body2 > max(body1 * 1.5, cfg.break_body_atr * atr)
                if not opposite or through or not touched or pullback_depth > cfg.max_retrace or expanding:
                    continue

                # The two-candle retest is valid and pending until continuation confirms.
                state = RetestState.PENDING
                reasons = ["two countertrend candles retested the broken level"]
                continuation_start = b + 3
                available_continuations = min(cfg.continuation_candles,
                                               latest - continuation_start + 1)
                continuation_ok = available_continuations > 0
                for offset in range(available_continuations):
                    candle = candles[continuation_start + offset]
                    co, cc = _num(candle, "open"), _num(candle, "close")
                    if direction == "up":
                        continuation_ok = continuation_ok and cc > co
                    else:
                        continuation_ok = continuation_ok and cc < co
                if not continuation_ok:
                    continue
                if available_continuations >= cfg.continuation_candles:
                    final = candles[continuation_start + cfg.continuation_candles - 1]
                    final_close = _num(final, "close")
                    pullback_high = max(_num(c, "high") for c in pullbacks)
                    pullback_low = min(_num(c, "low") for c in pullbacks)
                    if direction == "up" and final_close > pullback_high:
                        state = RetestState.CONFIRMED
                        reasons.append("two continuation candles confirmed above the pullback")
                    elif direction == "down" and final_close < pullback_low:
                        state = RetestState.CONFIRMED
                        reasons.append("two continuation candles confirmed below the pullback")
                    else:
                        continue
                else:
                    reasons.append("waiting for two continuation candles")

                zone = (level - cfg.zone_atr * atr, level + cfg.zone_atr * atr)
                candidates.append((b, RetestResult(state, direction, level, zone, tuple(reasons))))

        if not candidates:
            return RetestResult(RetestState.NONE, reasons=("no valid two-candle breakout retest",))
        return max(candidates, key=lambda item: item[0])[1]
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as exc:
        return RetestResult(RetestState.NONE, reasons=(f"filter safely skipped: {type(exc).__name__}",))


def apply_filter(m10_direction, result, mode="enforce"):
    """Only veto an opposing Momentum 10 signal; never creates a new signal."""
    direction = (m10_direction or "").lower()
    opposing = ((result.direction == "up" and direction == "put") or
                (result.direction == "down" and direction == "call"))
    should_veto = result.state in (RetestState.PENDING, RetestState.CONFIRMED) and opposing
    note = "WOULD_VETO" if should_veto else "PASS"
    if mode == "enforce" and should_veto:
        return None, "VETO"
    return m10_direction, note
