"""Configuration models, source chain and masked rendering (API §8, Tech Design DD-019).

Sources, later wins [REQ-202.a]: defaults → ``agentlens.toml`` (``--config`` path, else
``AGENTLENS_CONFIG``, else ``./agentlens.toml`` if present) → ``AGENTLENS_<SECTION>_<KEY>``
environment variables → command-line overrides.

- Secret settings (🔒 in API §8.2) also accept a ``*_FILE`` form: ``<ENV>_FILE`` in the
  environment, ``<key>_file`` in TOML [REQ-202.c]. A value and its file form in the same source is
  an error.
- Unknown TOML keys and unknown ``AGENTLENS_*`` variables are errors naming the key and its source
  [REQ-202.b]. So is every invalid value.
- :func:`render_config` prints the effective configuration with secrets masked [REQ-202.d].

Nothing here runs at import: loading happens in :func:`load_config`, called by ``configure()`` and
the CLI. Error messages never include a value (REQ-323).
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from datetime import timedelta
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    IPvAnyNetwork,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from reflex_agentlens.core.errors import ConfigError, ValidationError
from reflex_agentlens.core.timeutil import format_duration, parse_duration

__all__ = [
    "SECRET_KEYS",
    "AgentlensConfig",
    "format_size",
    "load_config",
    "parse_size",
    "render_config",
]

ENV_PREFIX = "AGENTLENS_"
CONFIG_ENV = "AGENTLENS_CONFIG"
DEFAULT_CONFIG_FILE = "agentlens.toml"
MASK = "***"

_SIZE_RE = re.compile(r"(\d+)\s*(B|KiB|MiB|GiB|TiB)?")
_SIZE_UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}
_API_KEY_RE = re.compile(r"al_[0-9a-hjkmnp-tv-z]{8}_[0-9A-Za-z]{32}")
_HOOK_RE = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*")


# ── value types ──────────────────────────────────────────────────────────────────────────────


def parse_size(value: object) -> int:
    """``"256KiB" | "8MiB" | "1GiB"`` or integer bytes."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValidationError("size", "expected a size string or integer bytes")
    if isinstance(value, int):
        if value < 0:
            raise ValidationError("size", "negative")
        return value
    m = _SIZE_RE.fullmatch(value.strip())
    if not m:
        raise ValidationError("size", "expected e.g. 256KiB, 8MiB, 1GiB or integer bytes")
    return int(m.group(1)) * _SIZE_UNITS[m.group(2) or "B"]


def format_size(n: int) -> str:
    for unit in ("TiB", "GiB", "MiB", "KiB"):
        size = _SIZE_UNITS[unit]
        if n and n % size == 0:
            return f"{n // size}{unit}"
    return str(n)


def _duration(value: object) -> timedelta:
    try:
        return parse_duration(value)
    except ValidationError as e:
        raise ValueError(e.reason) from None


def _size(value: object) -> int:
    try:
        return parse_size(value)
    except ValidationError as e:
        raise ValueError(e.reason) from None


def _csv_list(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


Duration = Annotated[timedelta, BeforeValidator(_duration)]
ByteSize = Annotated[int, BeforeValidator(_size), Field(ge=0)]
Port = Annotated[int, Field(ge=1, le=65535)]
PositiveInt = Annotated[int, Field(ge=1)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


# ── sections (API §8.2) ──────────────────────────────────────────────────────────────────────


class ServerConfig(_Section):
    http_host: str = "127.0.0.1"
    http_port: Port = 8000
    grpc_enabled: bool = True
    grpc_host: str | None = None  # None → same as http_host
    grpc_port: Port = 4317
    shutdown_grace: Duration = timedelta(seconds=20)
    public_url: AnyHttpUrl | None = None
    ingest_enabled: bool = True
    data_dir: Path | None = None  # None → $XDG_DATA_HOME/reflex-agentlens


class DatabaseConfig(_Section):
    url: SecretStr | None = None  # None → sqlite:///{data_dir}/agentlens.db
    auto_migrate: bool = True
    pool_size: PositiveInt = 10

    @field_validator("url")
    @classmethod
    def _check_url(cls, url: SecretStr | None) -> SecretStr | None:
        if url is not None:
            scheme = url.get_secret_value().split("://", 1)[0].split("+", 1)[0]
            if scheme not in {"sqlite", "postgresql", "postgres"}:
                raise ValueError("url must start with sqlite:// or postgresql://")
        return url


class IngestConfig(_Section):
    max_body_bytes: ByteSize = 8 << 20
    max_decompressed_bytes: ByteSize = 32 << 20
    queue_memory_bytes: ByteSize = 256 << 20
    overload_commit_latency: Duration = timedelta(seconds=2)
    overload_retry_after: Duration = timedelta(seconds=2)
    default_request_rate: PositiveInt = 50
    default_request_burst: PositiveInt = 100
    default_span_rate: PositiveInt = 20_000
    default_span_burst: PositiveInt = 40_000
    max_attribute_value_bytes: ByteSize = 256 << 10
    max_clock_skew: Duration = timedelta(hours=1)
    finalization_window: Duration = timedelta(minutes=5)
    regex_timeout: Duration = timedelta(milliseconds=25)
    redaction_hook: Annotated[str, Field(pattern=_HOOK_RE.pattern)] | None = None
    store_metrics: bool = True


class AuthConfig(_Section):
    mode: Literal["none", "password", "proxy"] | None = None
    admin_username: Annotated[str, Field(min_length=1)] = "admin"
    admin_password: SecretStr | None = None
    session_secret: SecretStr | None = None  # None → generated into {data_dir}/session_secret
    session_ttl: Duration = timedelta(hours=12)
    trusted_proxies: Annotated[list[IPvAnyNetwork], BeforeValidator(_csv_list)] = Field(
        default_factory=list
    )
    proxy_user_header: str = "X-Forwarded-User"
    proxy_groups_header: str = "X-Forwarded-Groups"
    proxy_admin_group: str | None = None
    frame_ancestors: str = "'self'"


class BootstrapConfig(_Section):
    api_key: SecretStr | None = None

    @field_validator("api_key")
    @classmethod
    def _check_key(cls, key: SecretStr | None) -> SecretStr | None:
        if key is not None and not _API_KEY_RE.fullmatch(key.get_secret_value()):
            raise ValueError("must have the al_<key_id>_<secret> format")
        return key


class RetentionConfig(_Section):
    purge_interval: Duration = timedelta(hours=1)
    default_max_age: Duration = timedelta(days=30)  # 0 = keep forever
    storage_size_limit: ByteSize | None = None
    purge_batch_traces: PositiveInt = 500


class LiveConfig(_Section):
    enabled: bool = True
    min_interval: Duration = timedelta(seconds=1)
    dashboard_min_interval: Duration = timedelta(seconds=10)


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"


class SelfTelemetryConfig(_Section):
    enabled: bool = False
    endpoint: AnyHttpUrl | None = None
    protocol: Literal["http/protobuf", "grpc"] = "http/protobuf"
    headers: SecretStr | None = None


class PricingConfig(_Section):
    file: Path | None = None  # None → bundled table


class DemoConfig(_Section):
    enabled: bool = False
    dataset: Path | None = None  # None → bundled sample dataset


class AgentlensConfig(BaseSettings):
    """Effective configuration. Build it with :func:`load_config`, not from the environment."""

    model_config = SettingsConfigDict(extra="forbid", frozen=True)

    server: ServerConfig = ServerConfig()
    database: DatabaseConfig = DatabaseConfig()
    ingest: IngestConfig = IngestConfig()
    auth: AuthConfig = AuthConfig()
    bootstrap: BootstrapConfig = BootstrapConfig()
    retention: RetentionConfig = RetentionConfig()
    live: LiveConfig = LiveConfig()
    logging: LoggingConfig = LoggingConfig()
    self_telemetry: SelfTelemetryConfig = SelfTelemetryConfig()
    pricing: PricingConfig = PricingConfig()
    demo: DemoConfig = DemoConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Only explicit input: the source chain lives in load_config (DD-019).
        return (init_settings,)

    @model_validator(mode="after")
    def _cross_checks(self) -> AgentlensConfig:
        password = self.auth.admin_password
        if self.auth.mode == "password" and (
            password is None or not password.get_secret_value().strip()  # blank = missing
        ):
            raise ValueError("auth.admin_password: required when auth.mode = password")
        if self.self_telemetry.enabled and self.self_telemetry.endpoint is None:
            raise ValueError("self_telemetry.endpoint: required when self-telemetry is enabled")
        return self


SECTIONS: dict[str, type[_Section]] = {
    name: field.annotation  # type: ignore[misc]
    for name, field in AgentlensConfig.model_fields.items()
}
SECRET_KEYS: frozenset[tuple[str, str]] = frozenset(
    (section, key)
    for section, model in SECTIONS.items()
    for key, field in model.model_fields.items()
    if "SecretStr" in str(field.annotation)
)


def _env_name(section: str, key: str) -> str:
    return f"{ENV_PREFIX}{section}_{key}".upper()


_SIZE_KEYS = frozenset(
    key
    for model in SECTIONS.values()
    for key in model.model_fields
    if key.endswith(("_bytes", "_limit"))
)
_ENV_KEYS: dict[str, tuple[str, str]] = {
    _env_name(section, key): (section, key)
    for section, model in SECTIONS.items()
    for key in model.model_fields
}


# ── loading ──────────────────────────────────────────────────────────────────────────────────

_Layer = dict[tuple[str, str], tuple[object, str]]


def _read_secret_file(path: str, key: str, source: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError:
        raise ConfigError(key, source, "secret file is not readable") from None


def _toml_layer(path: Path) -> _Layer:
    source = str(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ConfigError("--config", source, "file is not readable") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            "--config", source, "not valid TOML: " + str(e).split("(", 1)[-1].rstrip(")")
        ) from None
    layer: _Layer = {}
    for section, table in data.items():
        if section not in SECTIONS:
            raise ConfigError(section, source, "unknown section")
        if not isinstance(table, dict):
            raise ConfigError(section, source, "must be a table")
        fields = SECTIONS[section].model_fields
        for key, value in table.items():
            name = f"{section}.{key}"
            if key.endswith("_file") and (section, key[:-5]) in SECRET_KEYS:
                base = (section, key[:-5])
                if base in layer:
                    raise ConfigError(name, source, "set both as a value and as a file")
                if not isinstance(value, str):
                    raise ConfigError(name, source, "must be a path string")
                layer[base] = (_read_secret_file(value, name, source), f"{source} ({key})")
                continue
            if key not in fields:
                raise ConfigError(name, source, "unknown key")
            if (section, key) in layer:
                raise ConfigError(name, source, "set both as a value and as a file")
            layer[(section, key)] = (value, source)
    return layer


def _env_layer(env: Mapping[str, str]) -> _Layer:
    layer: _Layer = {}
    for name in sorted(env):
        if not name.startswith(ENV_PREFIX) or name == CONFIG_ENV:
            continue
        value = env[name]
        if name.endswith("_FILE") and _ENV_KEYS.get(name[:-5]) in SECRET_KEYS:
            target = _ENV_KEYS[name[:-5]]
            if name[:-5] in env:
                raise ConfigError(name, "environment", "set both as a value and as a file")
            layer[target] = (_read_secret_file(value, name, "environment"), f"environment {name}")
            continue
        if name not in _ENV_KEYS:
            raise ConfigError(name, "environment", "unknown AGENTLENS_* variable")
        layer[_ENV_KEYS[name]] = (value, f"environment {name}")
    return layer


def _cli_layer(overrides: Mapping[str, object], source: str = "command line") -> _Layer:
    layer: _Layer = {}
    for dotted, value in overrides.items():
        section, _, key = dotted.partition(".")
        if section not in SECTIONS or key not in SECTIONS[section].model_fields:
            raise ConfigError(dotted, source, "unknown setting")
        layer[(section, key)] = (value, source)
    return layer


def _default_data_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/share")
    return Path(base) / "reflex-agentlens"


def load_config(
    *,
    config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, object] | None = None,
    cwd: str | Path | None = None,
    command_defaults: Mapping[str, object] | None = None,
) -> AgentlensConfig:
    """Resolve the effective configuration; raise :class:`ConfigError` naming key and source.

    ``command_defaults`` replace built-in defaults for one command (``collect`` listens on
    4318) and sit below every other source.
    """
    env = os.environ if env is None else env
    toml_path: Path | None = None
    if config_path is not None:
        toml_path = Path(config_path)
    elif env.get(CONFIG_ENV):
        toml_path = Path(env[CONFIG_ENV])
    else:
        candidate = Path(cwd or Path.cwd()) / DEFAULT_CONFIG_FILE
        toml_path = candidate if candidate.is_file() else None

    merged: _Layer = {}
    for layer in (
        _cli_layer(command_defaults or {}, "command default"),
        _toml_layer(toml_path) if toml_path else {},
        _env_layer(env),
        _cli_layer(cli_overrides or {}),
    ):
        merged.update(layer)

    nested: dict[str, dict[str, object]] = {section: {} for section in SECTIONS}
    for (section, key), (value, _) in merged.items():
        nested[section][key] = value
    try:
        cfg = AgentlensConfig(**nested)  # type: ignore[arg-type]
    except PydanticValidationError as e:
        raise _config_error(e, merged) from None
    return _derive(cfg, env)


def _config_error(e: PydanticValidationError, merged: _Layer) -> ConfigError:
    err = e.errors(include_url=False, include_input=False)[0]
    loc = [str(part) for part in err["loc"]]
    msg = str(err["msg"]).removeprefix("Value error, ")
    if len(loc) >= 2:
        section, key = loc[0], loc[1]
    elif ":" in msg and "." in msg.split(":", 1)[0]:  # cross-field check names its key
        dotted, msg = (part.strip() for part in msg.split(":", 1))
        section, _, key = dotted.partition(".")
    else:
        section, key = (loc[0] if loc else "config"), ""
    source = merged.get((section, key), (None, "default"))[1]
    return ConfigError(f"{section}.{key}" if key else section, source, msg)


def _derive(cfg: AgentlensConfig, env: Mapping[str, str]) -> AgentlensConfig:
    server = cfg.server
    data_dir = server.data_dir or _default_data_dir(env)
    server = server.model_copy(
        update={"data_dir": data_dir, "grpc_host": server.grpc_host or server.http_host}
    )
    database = cfg.database
    if database.url is None:
        url = SecretStr(f"sqlite:///{data_dir / 'agentlens.db'}")
        database = database.model_copy(update={"url": url})
    return cfg.model_copy(update={"server": server, "database": database})


# ── rendering ────────────────────────────────────────────────────────────────────────────────


def _render_value(value: Any) -> str:
    if isinstance(value, SecretStr):
        return f'"{MASK}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, timedelta):
        return f'"{format_duration(value)}"'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(v) for v in value) + "]"
    if isinstance(value, IPv4Network | IPv6Network | Path | AnyHttpUrl | str):
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{text}"'
    raise TypeError(f"cannot render {type(value).__name__}")


def render_config(cfg: AgentlensConfig) -> str:
    """Effective configuration as TOML text with every secret shown as ``"***"`` [REQ-202.d]."""
    lines: list[str] = []
    for section in SECTIONS:
        model = getattr(cfg, section)
        lines.append(f"[{section}]")
        for key in type(model).model_fields:
            value = getattr(model, key)
            if value is None:
                lines.append(f"# {key} = (unset)")
            elif key in _SIZE_KEYS:
                lines.append(f'{key} = "{format_size(value)}"')
            else:
                lines.append(f"{key} = {_render_value(value)}")
        lines.append("")
    return "\n".join(lines)
