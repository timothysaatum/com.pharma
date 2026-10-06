"""Loyalty arithmetic shared by every server write path.

Before this module the award formula existed in exactly one place —
``SalesService.process_sale`` — and the offline path (``SaleProjector``) had none
at all, so a sale taken offline and synced later produced a sale row with no
points behind it. Two implementations of the same rule would drift again, so the
rule lives here once and both paths call it.

The single invariant this module exists to protect:

    customer.loyalty_tier == resolve_loyalty_tier(customer.loyalty_points)

``apply_loyalty_award`` is the only supported way to move points on the server.
It cannot leave the pair inconsistent, because it always derives the tier from the
new balance rather than accepting one from the caller.

NOT a ledger. ``loyalty_points`` is still a counter on the customer row; this
module makes the counter correct, not reconstructible. See ADR 0010.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.sales.utils.sale_helpers import (
    DEFAULT_LOYALTY_THRESHOLDS,
    resolve_loyalty_tier,
)

__all__ = [
    "DEFAULT_LOYALTY_THRESHOLDS",
    "LoyaltyAward",
    "apply_loyalty_award",
    "loyalty_enabled_for",
    "points_for_amount",
    "resolve_loyalty_tier",
]


def loyalty_enabled_for(organization: Any) -> bool:
    """True when this org has opted into the loyalty programme."""
    settings = getattr(organization, "settings", None) or {}
    if not isinstance(settings, dict):
        return False
    return bool(settings.get("enable_loyalty_program", False))


def _loyalty_config(organization: Any) -> Dict:
    settings = getattr(organization, "settings", None) or {}
    if not isinstance(settings, dict):
        return {}
    cfg = settings.get("loyalty", {})
    return cfg if isinstance(cfg, dict) else {}


def points_for_amount(total_amount: Any, organization: Any) -> int:
    """Points earned by ``total_amount`` at this org's rate.

    ``points_per_unit`` defaults to 1.0 and is applied to the post-discount
    total, which is what ``process_sale`` has always done. Truncated to a whole
    point, matching the existing behaviour exactly.
    """
    cfg = _loyalty_config(organization)
    try:
        rate = Decimal(str(cfg.get("points_per_unit", "1.0")))
    except Exception:  # pragma: no cover - malformed org setting
        rate = Decimal("1.0")
    amount = Decimal(str(total_amount or 0))
    return int(amount * rate)


def _thresholds(organization: Any) -> Dict:
    cfg = _loyalty_config(organization)
    return cfg.get("tier_thresholds") or DEFAULT_LOYALTY_THRESHOLDS


def _round2(value: Any) -> float:
    return float(round(Decimal(str(value or 0)), 2))


class LoyaltyAward:
    """The result of one award, in the shape the event payload needs."""

    __slots__ = ("loyalty_points", "loyalty_tier", "points_awarded", "tier_changed")

    def __init__(
        self,
        loyalty_points: int,
        loyalty_tier: str,
        points_awarded: int,
        tier_changed: bool,
    ) -> None:
        self.loyalty_points = loyalty_points
        self.loyalty_tier = loyalty_tier
        self.points_awarded = points_awarded
        self.tier_changed = tier_changed

    def snapshot(self) -> Dict[str, Any]:
        return {
            "loyalty_points": self.loyalty_points,
            "loyalty_tier": self.loyalty_tier,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"LoyaltyAward(points={self.loyalty_points}, tier={self.loyalty_tier!r}, "
            f"awarded={self.points_awarded})"
        )


async def apply_loyalty_award(
    db: AsyncSession,
    customer: Any,
    organization: Any,
    *,
    sale_id: Any,
    direction: str,
    total_amount: Any = None,
    increment_orders: bool = True,
) -> Optional[LoyaltyAward]:
    """Move a customer's loyalty by one sale, and keep the tier consistent.

    ``direction`` is ``"earn"`` or ``"refund"``. On ``earn`` the points are
    ``points_for_amount(total_amount, organization)`` and the denormalized order
    counters advance. On ``refund`` the caller has already deducted the points it
    wants removed via ``points_delta``; this function only re-derives the tier.

    Returns ``None`` — having changed nothing — when the org has loyalty
    disabled or the customer is a walk-in with no registered profile. Walk-in
    sales never earn.
    """
    if direction not in ("earn", "refund"):
        raise ValueError(f"direction must be 'earn' or 'refund', got {direction!r}")

    if not loyalty_enabled_for(organization):
        return None
    if customer is None:
        return None

    thresholds = _thresholds(organization)
    previous_points = int(customer.loyalty_points or 0)

    if direction == "earn":
        points_awarded = points_for_amount(total_amount, organization)
        if points_awarded <= 0:
            # A zero-value sale must not bump the counters or emit an event.
            return None
        customer.loyalty_points = previous_points + points_awarded
        if increment_orders:
            customer.total_orders = (customer.total_orders or 0) + 1
            customer.total_value = _round2(
                Decimal(str(customer.total_value or 0)) + Decimal(str(total_amount or 0))
            )
    else:
        points_awarded = 0

    # The invariant, enforced in one place: the tier is always derived from the
    # balance, never accepted from a caller.
    new_tier = resolve_loyalty_tier(customer.loyalty_points, thresholds)
    tier_changed = new_tier != customer.loyalty_tier
    customer.loyalty_tier = new_tier

    return LoyaltyAward(
        loyalty_points=int(customer.loyalty_points or 0),
        loyalty_tier=new_tier,
        points_awarded=points_awarded,
        tier_changed=tier_changed,
    )


def loyalty_payload(
    award: LoyaltyAward,
    *,
    customer_id: Any,
    sale_id: Any,
    direction: str,
    organization_id: Any,
    sale_number: Optional[str] = None,
    source: str = "online_sale",
) -> Dict[str, Any]:
    """Build the ``customer_loyalty_changed`` payload.

    Absolute post-state only. See ADR 0010 for why that makes duplicates safe.
    """
    payload: Dict[str, Any] = {
        "customer_id": str(customer_id),
        "organization_id": str(organization_id),
        "loyalty_points": award.loyalty_points,
        "loyalty_tier": award.loyalty_tier,
        "sale_id": str(sale_id),
        "direction": direction,
        "source": source,
    }
    if sale_number:
        payload["sale_number"] = sale_number
    return payload


def coerce_uuid(value: Any) -> Optional[uuid.UUID]:
    """Best-effort UUID parse. Returns None rather than raising on junk."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
