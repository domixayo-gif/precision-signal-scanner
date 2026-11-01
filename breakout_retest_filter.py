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

                p1, p2 = b + 1, b + 2
                if p1 > latest:
                    continue
                if p2 > latest:
                    first = candles[p1]
                    first_open, first_close = _num(first, "open"), _num(first, "close")
                    first_body = abs(first_close - first_open)
                    if direction == "up":
                        first_opposite = first_close < first_open
                        first_extreme = _num(first, "low")
                        first_touched = first_extreme <= level + cfg.zone_atr * atr
                        first_through = first_close < level - cfg.max_close_through_atr * atr
                        first_depth = (hi - first_extreme) / leg
                    else:
                        first_opposite = first_close > first_open
                        first_extreme = _num(first, "high")
                        first_touched = first_extreme >= level - cfg.zone_atr * atr
                        first_through = first_close > level + cfg.max_close_through_atr * atr
                        first_depth = (first_extreme - lo) / leg
                    if first_opposite and first_touched and not first_through and first_depth <= cfg.max_retrace:
                        zone = (level - cfg.zone_atr * atr, level + cfg.zone_atr * atr)
                        candidates.append((b, RetestResult(
                            RetestState.PENDING, direction, level, zone,
                            ("first countertrend candle reached the retest zone; waiting for the second pullback candle",)
                        )))
                    continue
                pullbacks = candles[p1:p2 + 1]
                o1, c1 = _num(pullbacks[0], "open"), _num(pullbacks[0], "close")
                o2, c2 = _num(pullbacks[1], "open"), _num(pullbacks[1], "close")
                if direction == "up":
                    opposite = c1 < o1 and c2 < o2
                    extreme = min(_num(c, "low") for c in pullbacks)
                    touched = extreme <= level + cfg.zone_atr * atr
                    pullback_depth = (hi - extreme) / leg
                else:
                    opposite = c1 > o1 and c2 > o2
                    extreme = max(_num(c, "high") for c in pullbacks)
                    touched = extreme >= level - cfg.zone_atr * atr
                    pullback_depth = (extreme - lo) / leg

                body1, body2 = abs(c1 - o1), abs(c2 - o2)
                expanding = body2 > max(body1 * 1.5, cfg.break_body_atr * atr)
                if not opposite or not touched or pullback_depth > cfg.max_retrace or expanding:
                    continue

                zone = (level - cfg.zone_atr * atr, level + cfg.zone_atr * atr)
                # Once the two-candle retest holds, keep its direction protected
                # until price closes decisively through the level or continuation confirms.
                invalidation_at = None
                invalidation_price = (level - cfg.max_close_through_atr * atr
                                      if direction == "up"
                                      else level + cfg.max_close_through_atr * atr)
                for candle_index in range(p1, latest + 1):
                    close = _num(candles[candle_index], "close")
                    broken = (close < invalidation_price if direction == "up"
                              else close > invalidation_price)
                    if broken:
                        invalidation_at = candle_index
                        break
                if invalidation_at is not None:
                    candidates.append((b, RetestResult(
                        RetestState.INVALIDATED, direction, level, zone,
                        ("price closed decisively through the retested level",)
                    )))
                    continue

                # Do not forget a valid retest just because the first candle(s)
                # after it are weak or mixed. Search for two consecutive
                # continuation candles anywhere inside the still-fresh setup.
                state = RetestState.PENDING
                reasons = ["two countertrend candles retested the broken level; level remains protected"]
                continuation_start = b + 3
                pullback_high = max(_num(c, "high") for c in pullbacks)
                pullback_low = min(_num(c, "low") for c in pullbacks)
                confirmed = False
                for second_index in range(continuation_start + 1, latest + 1):
                    first = candles[second_index - 1]
                    second = candles[second_index]
                    first_open, first_close = _num(first, "open"), _num(first, "close")
                    second_open, second_close = _num(second, "open"), _num(second, "close")
                    if direction == "up":
                        pair_continues = first_close > first_open and second_close > second_open
                        breaks_pullback = second_close > pullback_high
                    else:
                        pair_continues = first_close < first_open and second_close < second_open
                        breaks_pullback = second_close < pullback_low
                    if pair_continues and breaks_pullback:
                        state = RetestState.CONFIRMED
                        confirmed = True
                        break
                if confirmed:
                    reasons.append("two consecutive continuation candles confirmed beyond the pullback")
                else:
                    reasons.append("waiting for two consecutive continuation candles; weak/mixed candles do not cancel the retest")

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
