"""Span normalization; the pipeline itself lives in :mod:`.pipeline`.

``normalize_batch`` and friends are re-exported lazily: the redaction engine imports
``normalize.spans``, so an eager import of the pipeline here would make the package and the
engine import each other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reflex_agentlens.ingest.normalize.pipeline import (
        BatchContext,
        NormalizeResult,
        normalize_batch,
        normalize_span,
    )

__all__ = ["BatchContext", "NormalizeResult", "normalize_batch", "normalize_span"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from reflex_agentlens.ingest.normalize import pipeline

        return getattr(pipeline, name)
    raise AttributeError(name)
