"""
Projector base class + registry.

A projector reads an accepted event and writes read-model state to
Postgres. Per ADR 0009, projectors must be pure functions of
``(event, current_read_model)`` — no wall-clock reads, no
non-deterministic external state — so a replay from ``event_log``
reproduces the same read-model bit-for-bit.

Contract:

- ``validate(event, db)`` runs before ``apply``. It classifies the
  event as OK / DEFERRED (deps missing) / REJECTED_PERMANENT (business
  rule violation that cannot be resolved by retry). REJECTED_PERMANENT
  events are NOT written to ``event_log`` (see ADR 0007 failure
  classification).
- ``apply(event, db)`` writes the read-model state. Called only after
  ``validate`` returns OK.

Registry:

- Each concrete projector registers itself against one
  :class:`~app.schemas.event_envelope.AggregateType` via
  :meth:`ProjectorRegistry.register`. The router looks up projectors by
  aggregate type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Type

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import AggregateType, EventEnvelope


class ProjectorStatus(str, Enum):
    """Verdict returned by :meth:`Projector.validate`."""

    OK = "ok"
    """Event may be appended and applied."""

    DEFERRED = "deferred"
    """Event has unresolved dependencies. Router will park in
    ``pending_projections`` and revisit when a dependency lands.
    """

    REJECTED_PERMANENT = "rejected_permanent"
    """Event will never be applicable (invalid org scope, malformed
    payload, missing entity that will never exist, etc.). Event is
    NOT appended.
    """


@dataclass(slots=True)
class ProjectorResult:
    status: ProjectorStatus
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    unresolved_deps: Optional[list[str]] = None
    """Populated on DEFERRED — the specific ULIDs the projector is
    waiting for. May be a subset of ``event.dependencies`` if some
    dependencies are already resolved.
    """


class Projector(ABC):
    """Base class for per-aggregate-type projectors."""

    aggregate_type: AggregateType
    """Concrete subclasses set this class attribute."""

    @abstractmethod
    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        """Classify the event without writing state. Called before
        ``apply``. Must be free of side effects.
        """

    @abstractmethod
    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        """Write read-model state for a validated event. Must be
        idempotent — a replay of the same event MUST NOT produce a
        different read-model state.
        """


class ProjectorRegistry:
    """Aggregate type → Projector instance mapping."""

    _projectors: Dict[AggregateType, Projector] = {}

    @classmethod
    def register(cls, projector_cls: Type[Projector]) -> Type[Projector]:
        """Decorator or explicit call. Registers a projector class by
        its ``aggregate_type`` class attribute.
        """
        agg = projector_cls.aggregate_type
        if agg in cls._projectors:
            existing = type(cls._projectors[agg]).__name__
            raise ValueError(
                f"Projector already registered for {agg}: {existing}"
            )
        cls._projectors[agg] = projector_cls()
        return projector_cls

    @classmethod
    def get(cls, aggregate_type: AggregateType) -> Optional[Projector]:
        # Envelope may pass aggregate_type as the raw string when
        # use_enum_values=True is set on the pydantic model.
        if isinstance(aggregate_type, str):
            try:
                aggregate_type = AggregateType(aggregate_type)
            except ValueError:
                return None
        return cls._projectors.get(aggregate_type)

    @classmethod
    def _clear_for_tests(cls) -> None:
        cls._projectors.clear()
