"""Exception hierarchy shared by every layer (API §6.6).

Error hygiene (API §2, REQ-323): messages carry field names, entity kinds, IDs, counts and error
classes only — never attribute values, message content or keys.
"""

from __future__ import annotations

__all__ = [
    "AgentlensError",
    "ConfigError",
    "Conflict",
    "Forbidden",
    "NotFound",
    "StorageUnavailable",
    "ValidationError",
]


class AgentlensError(Exception):
    """Base class of every agentlens error."""


class ValidationError(AgentlensError):
    """Malformed filter, settings or protocol input. Pages show it as an inline field error."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid {field}: {reason}")


class NotFound(AgentlensError):
    """Missing or purged trace, session or project [REQ-103.b]."""

    def __init__(self, entity: str) -> None:
        self.entity = entity
        super().__init__(f"{entity} not found")


class Forbidden(AgentlensError):
    """Not admin, project not allowed, or demo mode."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"forbidden: {reason}")


class Conflict(AgentlensError):
    """Duplicate slug or filter name."""

    def __init__(self, entity: str) -> None:
        self.entity = entity
        super().__init__(f"{entity} already exists")


class StorageUnavailable(AgentlensError):
    """The database is unreachable [REQ-330]."""

    def __init__(self, reason: str = "database unreachable") -> None:
        self.reason = reason
        super().__init__(f"storage unavailable: {reason}")


class ConfigError(AgentlensError):
    """Invalid or unknown configuration; names the key and its source [REQ-202.b]."""

    def __init__(self, key: str, source: str, reason: str) -> None:
        self.key = key
        self.source = source
        self.reason = reason
        super().__init__(f"configuration error in {key} (from {source}): {reason}")
