"""core.errors: the exception hierarchy of API §6.6 (plan task P1-06.1)."""

from __future__ import annotations

import pytest

from reflex_agentlens.core import errors


@pytest.mark.parametrize(
    "exc",
    [
        errors.ValidationError("trace_id", "wrong length"),
        errors.NotFound("trace"),
        errors.Forbidden("requires admin"),
        errors.Conflict("project slug"),
        errors.StorageUnavailable(),
        errors.ConfigError("server.http_port", "env AGENTLENS_SERVER_HTTP_PORT", "not an int"),
    ],
)
def test_every_error_is_an_agentlens_error(exc: errors.AgentlensError) -> None:
    assert isinstance(exc, errors.AgentlensError)
    assert str(exc)


def test_validation_error_names_the_field_only() -> None:
    exc = errors.ValidationError("span_id", "all-zero")
    assert (exc.field, exc.reason) == ("span_id", "all-zero")
    assert str(exc) == "invalid span_id: all-zero"


def test_config_error_names_key_and_source() -> None:
    exc = errors.ConfigError("ingest.max_body_bytes", "agentlens.toml", "not a size")
    assert "ingest.max_body_bytes" in str(exc)
    assert "agentlens.toml" in str(exc)
