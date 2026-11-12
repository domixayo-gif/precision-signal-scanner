"""Payout-aware, capped three-attempt EMA recovery sizing.

The target is net profit over the whole sequence after recovering confirmed
losses. Payout is read fresh from the broker for every new order.
"""
from decimal import Decimal, ROUND_CEILING, InvalidOperation

SEQUENCE_NET_TARGET = Decimal("1.00")
MAX_STEPS = 3
MAX_SINGLE_STAKE = Decimal("33.74")

class StakeLimitExceeded(ValueError):
    pass

def normalize_payout(value):
    """Normalize broker payout as either a ratio (0.82) or percent (82)."""
    try:
        payout = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Broker payout is missing or invalid.")
    if not payout.is_finite():
        raise ValueError("Broker payout is missing or invalid.")
    if Decimal("1") < payout <= Decimal("100"):
        payout /= Decimal("100")
    if payout <= 0 or payout > 1:
        raise ValueError("Broker payout must be greater than 0% and at most 100%.")
    return payout

def stake_for_recovery(confirmed_losses, payout, target=SEQUENCE_NET_TARGET,
                       max_stake=MAX_SINGLE_STAKE):
    """Return the cent-rounded-up stake needed to recover losses plus target."""
    try:
        losses = Decimal(str(confirmed_losses))
        goal = Decimal(str(target))
        cap = Decimal(str(max_stake))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Losses, target, and cap must be valid amounts.")
    if not losses.is_finite() or losses < 0 or not goal.is_finite() or goal <= 0:
        raise ValueError("Losses must be non-negative and target must be positive.")
    if not cap.is_finite() or cap <= 0:
        raise ValueError("Maximum stake must be positive.")
    rate = normalize_payout(payout)
    raw = (losses + goal) / rate
    stake = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    if stake > cap:
        raise StakeLimitExceeded(f"Required stake {stake} exceeds the per-trade cap of {cap}.")
    return float(stake)

def broker_turbo_payout(client, broker_asset):
    """Read the exact broker asset current TURBO payout as a decimal ratio."""
    getter = getattr(client, "get_all_profit", None)
    if not callable(getter):
        raise RuntimeError("IQ Option API does not expose get_all_profit().")
    profits = getter()
    if not isinstance(profits, dict):
        raise RuntimeError("Broker returned an invalid payout table.")
    asset_data = profits.get(str(broker_asset))
    if not isinstance(asset_data, dict):
        raise RuntimeError(f"No current payout returned for {broker_asset}.")
    if "turbo" not in asset_data:
        raise RuntimeError(f"No TURBO payout returned for {broker_asset}.")
    return float(normalize_payout(asset_data["turbo"]))
