"""core.config: models, source chain, *_FILE secrets and masked rendering.

Plan tasks P1-07.1, P1-07.2 and P1-07.3 (API §8, Tech Design DD-019).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from reflex_agentlens.core import config
from reflex_agentlens.core.config import AgentlensConfig, load_config, render_config
from reflex_agentlens.core.errors import ConfigError

VALID_KEY = "al_k7m2q9xd_3JcV0pXwQ8rT1nZs6bLk2yHe9GdF4aMu"
CANARY = "c4n4ry-s3cr3t-v4lu3"


def _load(
    tmp_path: Path, toml: str | None = None, env: dict[str, str] | None = None, **kw: object
) -> AgentlensConfig:
    path = None
    if toml is not None:
        path = tmp_path / "agentlens.toml"
        path.write_text(toml)
    base = {"HOME": str(tmp_path)}
    return load_config(config_path=path, env={**base, **(env or {})}, cwd=tmp_path, **kw)  # type: ignore[arg-type]


# ── P1-07.1: models ──────────────────────────────────────────────────────────────────────────

DEFAULTS = [
    ("server", "http_host", "127.0.0.1"),
    ("server", "http_port", 8000),
    ("server", "grpc_enabled", True),
    ("server", "grpc_port", 4317),
    ("server", "shutdown_grace", timedelta(seconds=20)),
    ("server", "public_url", None),
    ("server", "ingest_enabled", True),
    ("database", "auto_migrate", True),
    ("database", "pool_size", 10),
    ("ingest", "max_body_bytes", 8 * 1024 * 1024),
    ("ingest", "max_decompressed_bytes", 32 * 1024 * 1024),
    ("ingest", "queue_memory_bytes", 256 * 1024 * 1024),
    ("ingest", "overload_commit_latency", timedelta(seconds=2)),
    ("ingest", "overload_retry_after", timedelta(seconds=2)),
    ("ingest", "default_request_rate", 50),
    ("ingest", "default_request_burst", 100),
    ("ingest", "default_span_rate", 20_000),
    ("ingest", "default_span_burst", 40_000),
    ("ingest", "max_attribute_value_bytes", 256 * 1024),
    ("ingest", "finalization_window", timedelta(minutes=5)),
    ("ingest", "regex_timeout", timedelta(milliseconds=25)),
    ("ingest", "redaction_hook", None),
    ("ingest", "store_metrics", True),
    ("auth", "mode", None),
    ("auth", "admin_username", "admin"),
    ("auth", "admin_password", None),
    ("auth", "session_secret", None),
    ("auth", "session_ttl", timedelta(hours=12)),
    ("auth", "trusted_proxies", []),
    ("auth", "proxy_user_header", "X-Forwarded-User"),
    ("auth", "proxy_groups_header", "X-Forwarded-Groups"),
    ("auth", "proxy_admin_group", None),
    ("auth", "frame_ancestors", "'self'"),
    ("bootstrap", "api_key", None),
    ("retention", "purge_interval", timedelta(hours=1)),
    ("retention", "default_max_age", timedelta(days=30)),
    ("retention", "storage_size_limit", None),
    ("retention", "purge_batch_traces", 500),
    ("live", "enabled", True),
    ("live", "min_interval", timedelta(seconds=1)),
    ("live", "dashboard_min_interval", timedelta(seconds=10)),
    ("logging", "level", "INFO"),
    ("logging", "format", "json"),
    ("self_telemetry", "enabled", False),
    ("self_telemetry", "endpoint", None),
    ("self_telemetry", "protocol", "http/protobuf"),
    ("self_telemetry", "headers", None),
    ("pricing", "file", None),
    ("demo", "enabled", False),
    ("demo", "dataset", None),
]


def test_every_setting_of_api_8_2_is_modelled() -> None:
    modelled = {(s, k) for s, m in config.SECTIONS.items() for k in m.model_fields}
    assert modelled == {(s, k) for s, k, _ in DEFAULTS} | {
        ("server", "grpc_host"),
        ("server", "data_dir"),
        ("database", "url"),
    }


@pytest.mark.parametrize(("section", "key", "expected"), DEFAULTS)
def test_defaults_equal_api_8_2(tmp_path: Path, section: str, key: str, expected: object) -> None:
    cfg = _load(tmp_path)
    assert getattr(getattr(cfg, section), key) == expected


def test_derived_defaults(tmp_path: Path) -> None:
    cfg = _load(tmp_path, env={"XDG_DATA_HOME": str(tmp_path / "xdg")})
    assert cfg.server.data_dir == tmp_path / "xdg" / "reflex-agentlens"
    assert cfg.server.grpc_host == "127.0.0.1"
    assert cfg.database.url is not None
    assert cfg.database.url.get_secret_value() == (
        f"sqlite:///{tmp_path / 'xdg' / 'reflex-agentlens' / 'agentlens.db'}"
    )
    cfg = _load(tmp_path)  # no XDG_DATA_HOME → ~/.local/share
    assert cfg.server.data_dir == tmp_path / ".local/share/reflex-agentlens"


def test_secret_fields_are_typed_as_secrets() -> None:
    assert {
        ("database", "url"),
        ("auth", "admin_password"),
        ("auth", "session_secret"),
        ("bootstrap", "api_key"),
        ("self_telemetry", "headers"),
    } == config.SECRET_KEYS


INVALID = [
    ("server", "http_port", "70000"),
    ("server", "http_port", "abc"),
    ("server", "grpc_enabled", "maybe"),
    ("server", "shutdown_grace", "20 seconds"),
    ("server", "public_url", "not a url"),
    ("database", "url", "mysql://x"),
    ("database", "pool_size", "0"),
    ("ingest", "max_body_bytes", "8MB"),
    ("ingest", "regex_timeout", "-1s"),
    ("ingest", "redaction_hook", "no_colon"),
    ("auth", "mode", "oauth"),
    ("auth", "trusted_proxies", "10.0.0.0/33"),
    ("bootstrap", "api_key", "al_short"),
    ("logging", "level", "TRACE"),
    ("self_telemetry", "protocol", "http/json"),
]


@pytest.mark.parametrize(("section", "key", "value"), INVALID)
def test_req_202b_invalid_value_names_key_and_source(
    tmp_path: Path, section: str, key: str, value: str
) -> None:
    env_name = f"AGENTLENS_{section}_{key}".upper()
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, env={env_name: value})
    assert info.value.key == f"{section}.{key}"
    assert info.value.source == f"environment {env_name}"
    assert value not in str(info.value) or value in {"0"}


def test_req_202b_invalid_toml_value_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, toml='[ingest]\nmax_body_bytes = "huge"\n')
    assert info.value.key == "ingest.max_body_bytes"
    assert info.value.source == str(tmp_path / "agentlens.toml")


def test_cross_field_checks_name_the_missing_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, env={"AGENTLENS_AUTH_MODE": "password"})
    assert info.value.key == "auth.admin_password"
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, env={"AGENTLENS_SELF_TELEMETRY_ENABLED": "true"})
    assert info.value.key == "self_telemetry.endpoint"


def test_values_are_parsed_from_strings(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        env={
            "AGENTLENS_SERVER_GRPC_ENABLED": "false",
            "AGENTLENS_INGEST_MAX_BODY_BYTES": "1MiB",
            "AGENTLENS_AUTH_TRUSTED_PROXIES": "10.0.0.0/8, 192.168.0.0/16",
            "AGENTLENS_RETENTION_DEFAULT_MAX_AGE": "0",
            "AGENTLENS_BOOTSTRAP_API_KEY": VALID_KEY,
        },
    )
    assert cfg.server.grpc_enabled is False
    assert cfg.ingest.max_body_bytes == 1 << 20
    assert [str(n) for n in cfg.auth.trusted_proxies] == ["10.0.0.0/8", "192.168.0.0/16"]
    assert cfg.retention.default_max_age == timedelta(0)


# ── P1-07.2: sources ─────────────────────────────────────────────────────────────────────────


def test_req_202a_precedence_default_toml_env_cli(tmp_path: Path) -> None:
    toml = "[server]\nhttp_port = 9001\ngrpc_port = 9002\nshutdown_grace = 5\n"
    env = {"AGENTLENS_SERVER_GRPC_PORT": "9003", "AGENTLENS_SERVER_SHUTDOWN_GRACE": "6s"}
    cfg = _load(tmp_path, toml, env, cli_overrides={"server.shutdown_grace": "7s"})
    assert cfg.server.http_host == "127.0.0.1"  # default
    assert cfg.server.http_port == 9001  # TOML
    assert cfg.server.grpc_port == 9003  # env over TOML
    assert cfg.server.shutdown_grace == timedelta(seconds=7)  # CLI over env


def test_req_202a_config_path_from_env_and_cwd(tmp_path: Path) -> None:
    other = tmp_path / "other.toml"
    other.write_text("[server]\nhttp_port = 9100\n")
    cfg = load_config(env={"AGENTLENS_CONFIG": str(other), "HOME": str(tmp_path)}, cwd=tmp_path)
    assert cfg.server.http_port == 9100
    (tmp_path / "agentlens.toml").write_text("[server]\nhttp_port = 9200\n")
    assert load_config(env={"HOME": str(tmp_path)}, cwd=tmp_path).server.http_port == 9200


def test_req_202b_unknown_toml_key_and_section(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, "[server]\nhttp_prot = 1\n")
    assert (info.value.key, info.value.source) == (
        "server.http_prot",
        str(tmp_path / "agentlens.toml"),
    )
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, "[sever]\nhttp_port = 1\n")
    assert info.value.key == "sever"


def test_req_202b_unknown_env_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, env={"AGENTLENS_SERVER_HTTP_PROT": "1"})
    assert (info.value.key, info.value.source) == ("AGENTLENS_SERVER_HTTP_PROT", "environment")


def test_req_202b_unknown_cli_override(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, cli_overrides={"server.nope": 1})
    assert info.value.source == "command line"


def test_req_202b_malformed_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, "[server\n")
    assert info.value.source == str(tmp_path / "agentlens.toml")


SECRETS = [
    ("database", "url", "postgresql://agentlens:" + CANARY + "@db/agentlens"),
    ("auth", "admin_password", CANARY),
    ("auth", "session_secret", CANARY),
    ("bootstrap", "api_key", VALID_KEY),
    ("self_telemetry", "headers", "Authorization=Bearer%20" + CANARY),
]


@pytest.mark.parametrize(("section", "key", "value"), SECRETS)
def test_req_202c_every_secret_accepts_an_env_file(
    tmp_path: Path, section: str, key: str, value: str
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(value + "\n")  # Docker secrets usually end with a newline
    env_name = f"AGENTLENS_{section}_{key}".upper()
    cfg = _load(tmp_path, env={f"{env_name}_FILE": str(secret_file)})
    assert getattr(getattr(cfg, section), key).get_secret_value() == value


@pytest.mark.parametrize(("section", "key", "value"), SECRETS)
def test_req_202c_every_secret_accepts_a_toml_file_key(
    tmp_path: Path, section: str, key: str, value: str
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(value)
    cfg = _load(tmp_path, f'[{section}]\n{key}_file = "{secret_file}"\n')
    assert getattr(getattr(cfg, section), key).get_secret_value() == value


def test_req_202c_value_and_file_together_is_an_error(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("x")
    with pytest.raises(ConfigError):
        _load(
            tmp_path,
            env={
                "AGENTLENS_AUTH_ADMIN_PASSWORD": "x",
                "AGENTLENS_AUTH_ADMIN_PASSWORD_FILE": str(secret_file),
            },
        )


def test_req_202c_missing_secret_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, env={"AGENTLENS_AUTH_ADMIN_PASSWORD_FILE": str(tmp_path / "nope")})
    assert info.value.key == "AGENTLENS_AUTH_ADMIN_PASSWORD_FILE"


def test_file_suffix_is_only_for_secrets(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, env={"AGENTLENS_SERVER_HTTP_PORT_FILE": "/dev/null"})


def test_no_io_at_import() -> None:
    import importlib
    import sys

    sys.modules.pop("reflex_agentlens.core.config", None)
    module = importlib.import_module("reflex_agentlens.core.config")
    assert not hasattr(module, "CONFIG")  # nothing is loaded at import time


# ── P1-07.3: rendering ───────────────────────────────────────────────────────────────────────


def test_req_202d_rendered_config_masks_secrets_from_values_and_files(tmp_path: Path) -> None:
    secret_file = tmp_path / "pw"
    secret_file.write_text(CANARY + "-file")
    cfg = _load(
        tmp_path,
        toml=f'[database]\nurl = "postgresql://u:{CANARY}-toml@db/x"\n',
        env={
            "AGENTLENS_AUTH_MODE": "password",
            "AGENTLENS_AUTH_ADMIN_PASSWORD_FILE": str(secret_file),
            "AGENTLENS_AUTH_SESSION_SECRET": CANARY + "-env",
            "AGENTLENS_BOOTSTRAP_API_KEY": VALID_KEY,
            "AGENTLENS_SELF_TELEMETRY_ENABLED": "true",
            "AGENTLENS_SELF_TELEMETRY_ENDPOINT": "http://collector:4318",
            "AGENTLENS_SELF_TELEMETRY_HEADERS": "x=" + CANARY,
        },
    )
    text = render_config(cfg)
    assert CANARY not in text
    assert VALID_KEY not in text
    assert 'admin_password = "***"' in text
    assert 'url = "***"' in text
    assert 'api_key = "***"' in text


def test_rendered_config_is_valid_toml_that_reloads(tmp_path: Path) -> None:
    import tomllib

    cfg = _load(tmp_path, env={"AGENTLENS_AUTH_TRUSTED_PROXIES": "10.0.0.0/8"})
    data = tomllib.loads(render_config(cfg))
    assert data["ingest"]["max_body_bytes"] == "8MiB"
    assert data["server"]["shutdown_grace"] == "20s"
    assert data["auth"]["trusted_proxies"] == ["10.0.0.0/8"]
    assert "public_url" not in data["server"]  # unset values are comments
    data["database"].pop("url")  # masked
    path = tmp_path / "rendered.toml"
    lines = [
        f"[{s}]\n" + "".join(f"{k} = {_toml(v)}\n" for k, v in t.items()) for s, t in data.items()
    ]
    path.write_text("\n".join(lines))
    assert load_config(config_path=path, env={"HOME": str(tmp_path)}) == cfg


def _toml(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return f'"{v}"'
