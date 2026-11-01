"""Redaction hooks loaded by ``module:function`` in the tests (REQ-039.e)."""

from __future__ import annotations

from typing import Any


def mask_user(span: dict[str, Any]) -> dict[str, Any]:
    if "user.id" in span["attributes"]:
        span["attributes"]["user.id"] = "[REDACTED:hook]"
    return span


def no_change(span: dict[str, Any]) -> None:
    return None


def boom(span: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("secret content must not leak into the error: sk-abc")


NOT_CALLABLE = 3


class Nested:
    @staticmethod
    def hook(span: dict[str, Any]) -> None:
        return None
