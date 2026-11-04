"""Small, testable guard for overlapping binary-option exposure."""
from dataclasses import dataclass
from typing import Dict


@dataclass
class OpenTrade:
    order_id: str
    asset: str
    direction: str
    accepted_at: float
    expires_at: float


class TradeExposureGuard:
    """Limit concurrent orders globally and prevent same-asset overlap."""

    def __init__(self, max_open_trades=3, expiry_seconds=120, expiry_grace_seconds=10):
        if max_open_trades < 1 or expiry_seconds < 1 or expiry_grace_seconds < 0:
            raise ValueError("Exposure limits must be positive.")
        self.max_open_trades = int(max_open_trades)
        self.expiry_seconds = int(expiry_seconds)
        self.expiry_grace_seconds = int(expiry_grace_seconds)
        self.open_trades: Dict[str, OpenTrade] = {}

    def prune(self, now):
        now = float(now)
        expired = [order_id for order_id, trade in self.open_trades.items()
                   if trade.expires_at <= now]
        for order_id in expired:
            self.open_trades.pop(order_id, None)
        return expired

    def can_open(self, asset, now):
        self.prune(now)
        if any(trade.asset == str(asset) for trade in self.open_trades.values()):
            return False, "same asset already has an open trade"
        if len(self.open_trades) >= self.max_open_trades:
            return False, "maximum concurrent trade limit reached"
        return True, "capacity available"

    def register(self, order_id, asset, direction, accepted_at, expiry_seconds=None):
        key = str(order_id)
        if not key or key in self.open_trades:
            raise ValueError("Order ID must be unique and non-empty.")
        accepted_at = float(accepted_at)
        duration = self.expiry_seconds if expiry_seconds is None else int(expiry_seconds)
        if duration < 1:
            raise ValueError("Trade expiry must be positive.")
        self.open_trades[key] = OpenTrade(
            order_id=key,
            asset=str(asset),
            direction=str(direction).lower(),
            accepted_at=accepted_at,
            expires_at=accepted_at + duration + self.expiry_grace_seconds,
        )

    def count(self, now):
        self.prune(now)
        return len(self.open_trades)

    def is_asset_open(self, asset, now):
        self.prune(now)
        return any(trade.asset == str(asset) for trade in self.open_trades.values())


def classify_profit(net_profit):
    """Return WIN/LOSS/DRAW from broker-reported net profit."""
    value = float(net_profit)
    if value > 0:
        return "WIN"
    if value < 0:
        return "LOSS"
    return "DRAW"
