"""Login and logout routes, login throttle, CORS origin (plan task P5-04.2).

API §4 (login table), REQ-224.a/d, REQ-321.a, REQ-325, Tech Design §6.1 (Amendment A4).

- Mounted in ``password`` mode only; elsewhere the paths do not exist (404).
- CSRF guard: the ``Origin`` (or ``Referer``) host must be the request host or the host of
  ``server.public_url``; otherwise 403. ``SameSite=Lax`` on the cookie covers the rest.
- ``next`` is honoured only as a same-origin relative path (no open redirect); otherwise ``/``.
- Credentials are compared in constant time and the response never says which one was wrong.
- Throttle (REQ-224.d): after 5 failures from one IP within a minute, every further attempt waits
  at least 1 s before it is evaluated.
- The form body is parsed here (``application/x-www-form-urlencoded``, at most 8 KiB), so no
  multipart parser is needed.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from reflex_agentlens.app.auth import COOKIE, session_cookie
from reflex_agentlens.app.policy import effective_auth_mode
from reflex_agentlens.core.config import AgentlensConfig

__all__ = ["LOGIN_PATH", "LOGOUT_PATH", "LoginThrottle", "cors_origin", "login_routes", "safe_next"]

LOGIN_PATH = "/_agentlens/auth/login"
LOGOUT_PATH = "/_agentlens/auth/logout"
LOGIN_PAGE = "/login"
MAX_FORM_BYTES = 8 << 10


class LoginThrottle:
    """Failed logins per client IP over a sliding minute (bounded memory)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window_s: float = 60.0,
        free_failures: int = 5,
        delay_s: float = 1.0,
        max_clients: int = 10_000,
    ) -> None:
        self.clock = clock
        self.window_s = window_s
        self.free_failures = free_failures
        self.delay_s = delay_s
        self.max_clients = max_clients
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, ip: str) -> deque[float]:
        times = self._failures.get(ip)
        if times is None:
            return deque()
        cutoff = self.clock() - self.window_s
        while times and times[0] < cutoff:
            times.popleft()
        if not times:
            del self._failures[ip]
        return times

    def delay(self, ip: str) -> float:
        return self.delay_s if len(self._recent(ip)) >= self.free_failures else 0.0

    def failed(self, ip: str) -> None:
        times = self._recent(ip)
        times.append(self.clock())
        self._failures[ip] = times
        self._failures.move_to_end(ip)
        while len(self._failures) > self.max_clients:
            self._failures.popitem(last=False)

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)


def safe_next(value: str | None) -> str:
    """A same-origin relative path, or ``/``."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(not ch.isprintable() for ch in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def cors_origin(cfg: AgentlensConfig) -> str:
    """The one allowed CORS origin: ``server.public_url``'s, else the bind address's."""
    server = cfg.server
    if server.public_url is not None:
        parts = urlsplit(str(server.public_url))
        return f"{parts.scheme}://{parts.netloc}"
    host = server.http_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{server.http_port}"


def _same_origin(request: Request, cfg: AgentlensConfig) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return False
    netloc = urlsplit(source).netloc.lower()
    allowed = {request.headers.get("host", "").lower()}
    if cfg.server.public_url is not None:
        allowed.add(urlsplit(str(cfg.server.public_url)).netloc.lower())
    return bool(netloc) and netloc in allowed


def _secure(cfg: AgentlensConfig, request: Request) -> bool:
    public = cfg.server.public_url
    if public is not None:
        return urlsplit(str(public)).scheme == "https"
    return request.url.scheme == "https"


async def _form(request: Request) -> dict[str, str] | None:
    """First value of each field; ``None`` when the body is too large."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            return None
    fields = parse_qs(body.decode("utf-8", "replace"), max_num_fields=16)
    return {name: values[0] for name, values in fields.items() if values}


def login_routes(
    cfg: AgentlensConfig,
    key: bytes,
    *,
    throttle: LoginThrottle | None = None,
    now_s: Callable[[], int] = lambda: int(time.time()),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[Route]:
    """The two routes of API §4 in ``password`` mode; nothing in the other modes."""
    if effective_auth_mode(cfg) != "password":
        return []
    admin_password = cfg.auth.admin_password
    if admin_password is None:  # load_config refuses this; a hand-built config may not
        return []
    expected_user = cfg.auth.admin_username.encode()
    expected_password = admin_password.get_secret_value().encode()
    ttl = int(cfg.auth.session_ttl.total_seconds())
    limiter = throttle or LoginThrottle()

    async def login(request: Request) -> Response:
        if not _same_origin(request, cfg):
            return PlainTextResponse("forbidden", 403)
        ip = request.client.host if request.client else ""
        wait = limiter.delay(ip)
        if wait > 0:
            await sleep(wait)
        form = await _form(request)
        if form is None:
            return PlainTextResponse("request too large", 413)
        user = form.get("username", "").encode()
        password = form.get("password", "").encode()
        user_ok = hmac.compare_digest(user, expected_user)
        password_ok = hmac.compare_digest(password, expected_password)
        if not (user_ok and password_ok):
            limiter.failed(ip)
            return RedirectResponse(f"{LOGIN_PAGE}?error=1", 303)
        limiter.succeeded(ip)
        response = RedirectResponse(safe_next(form.get("next")), 303)
        response.set_cookie(
            COOKIE,
            session_cookie(key, cfg.auth.admin_username, now_s(), ttl),
            max_age=ttl,
            path="/",
            secure=_secure(cfg, request),
            httponly=True,
            samesite="lax",
        )
        return response

    async def logout(request: Request) -> Response:
        if not _same_origin(request, cfg):
            return PlainTextResponse("forbidden", 403)
        response = RedirectResponse(LOGIN_PAGE, 303)
        response.delete_cookie(
            COOKIE, path="/", secure=_secure(cfg, request), httponly=True, samesite="lax"
        )
        return response

    return [
        Route(LOGIN_PATH, login, methods=["POST"]),
        Route(LOGOUT_PATH, logout, methods=["POST"]),
    ]
