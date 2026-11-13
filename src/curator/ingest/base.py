"""Ingestion-source contract: the ABC every collector implements.

Why this shape:
- One abstract method (``fetch_new``) is enough; callers just consume it.
- ``RawItem`` is the only thing that crosses the collector -> storage
  boundary. Each collector converts its source-native records into this
  model before yielding, so source-specific types never leak.
- Source-specific extras live in ``RawItem.metadata`` so the core schema
  stays narrow.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class RawItem(BaseModel):
    """One collected piece of content, before any interpretation.

    Identity for dedupe is ``(source_id, external_id)``. The store assigns
    ``source_id``; collectors only fill ``external_id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str = Field(..., min_length=1, description="Source-native unique id (guid, URL, ...)")
    title: str = Field("", description="Headline; empty if the source has none")
    body: str = Field(..., description="Raw text or HTML; the store saves a plain-text copy")
    url: str = Field(..., description="Permalink back to the original")
    published_at: datetime = Field(..., description="When the source recorded it (tz-aware, UTC preferred)")
    lang: str | None = Field(None, description="ISO 639-1 code if known")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Source-specific extras")


class FetchError(RuntimeError):
    """The source could not be fetched at all (network error, bad status).

    Raised instead of silently yielding nothing so the caller can tell
    "no new items" apart from "fetch failed" and leave ``last_fetched_at``
    untouched in the second case.
    """


class IngestSource(ABC):
    """Abstract base class for collectors.

    ``name`` is the short string stored in ``sources.kind``. Instances
    should be cheap to build; the work happens inside ``fetch_new``.
    """

    name: ClassVar[str]

    @abstractmethod
    def fetch_new(self, source_config: dict[str, Any], since: datetime | None) -> Iterator[RawItem]:
        """Yield items newer than ``since`` (or everything available if None).

        Callers dedupe through ``Store.record_item`` and derive ``since``
        from ``sources.last_fetched_at``. Implementations raise
        ``FetchError`` when the source is unreachable.
        """
        ...
