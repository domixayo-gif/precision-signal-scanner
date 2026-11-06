"""EMA-only three-step martingale sizing and progression rules.

The scanner advances only after a broker-confirmed LOSS. Unknown results never
advance the sequence. A win or draw ends the sequence; the next fresh signal
starts again at step one.
"""

EMA_MARTINGALE_STAKES = (1.0, 2.0, 4.0)
MAX_STEPS = len(EMA_MARTINGALE_STAKES)


def stake_for_step(step):
    """Return the exact stake for 1-based martingale step 1, 2, or 3."""
    try:
        index = int(step)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Martingale step must be 1, 2, or 3.")
    if index < 1 or index > MAX_STEPS or index != step:
        raise ValueError("Martingale step must be 1, 2, or 3.")
    return EMA_MARTINGALE_STAKES[index - 1]


def next_step_after_result(outcome, current_step):
    """Advance only on a confirmed loss; never exceed the third $4 trade."""
    step = int(current_step)
    stake_for_step(step)  # validate range
    result = str(outcome).upper()
    if result == "LOSS" and step < MAX_STEPS:
        return step + 1
    if result in ("WIN", "DRAW", "LOSS"):
        return None
    raise ValueError("Outcome must be a broker-confirmed WIN, LOSS, or DRAW.")
