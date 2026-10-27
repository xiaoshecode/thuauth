"""Bounded, explicitly dual-stack transport for the Tsinghua Srun portal.

Request flow follows z4yx/GoAuthing libauth/requests.go (GPL-3.0-only),
commit 3730e252ca11e6abbd055d7850c417c5751c1efa; see NOTICE.md.
The reference does not document an offline marker or the IPv6 portal HTML.
Only the explicit ``not_online`` marker is accepted here; its availability,
IPv6 HTML, discovery redirects and double_stack behavior need campus testing.
"""

from __future__ import annotations

import asyncio
import errno
import html
import ipaddress
import math
import re
import socket
import ssl
from collections.abc import Callable, Iterable, Mapping
from types import TracebackType
from typing import Self

import aiohttp
from aiohttp.client_middlewares import ClientHandlerType
from yarl import URL

from thuauth.errors import NetworkError, ProtocolError
from thuauth.models import (
    AccessContext,
    AuthReply,
    Credentials,
    IPFamily,
    OnlineObservation,
    OnlineState,
)
from thuauth.protocol.srun import (
    CALLBACK,
    MAX_RESPONSE_BYTES,
    build_challenge_params,
    build_login_params,
    build_logout_params,
    parse_auth_reply,
    parse_challenge,
    parse_jsonp,
)

_FAMILIES = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}
_BASE_URLS = {
    "ipv4": "https://auth4.tsinghua.edu.cn",
    "ipv6": "https://auth6.tsinghua.edu.cn",
}
_DISCOVERY_URLS = {
    "ipv4": "http://login.tsinghua.edu.cn/index_1.html",
    "ipv6": "http://mirrors6.tuna.tsinghua.edu.cn/",
}
_READ_BUFFER = 16384
_REDIRECTS = {301, 302, 303, 307, 308}
_AC_ID = re.compile(r"(?<![\w])(?:ac_id\s*=\s*|index_)([0-9]{1,16})(?![\w])")
_PORTAL_IP = re.compile(
    r"(?<![\w])(?:ip|\"ip\"|'ip')\s*:\s*(?P<quote>[\"'])(?P<ip>[^\"'\r\n]{1,64})(?P=quote)"
)
_NO_ROUTE = {
    errno.EAFNOSUPPORT,
    errno.EADDRNOTAVAIL,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    # Winsock codes may appear as either errno or winerror in Python/aiohttp.
    10047,  # WSAEAFNOSUPPORT
    10049,  # WSAEADDRNOTAVAIL
    10050,  # WSAENETDOWN
    10051,  # WSAENETUNREACH
    10065,  # WSAEHOSTUNREACH
}


def _timeout(seconds: float) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=seconds,
        connect=seconds,
        sock_connect=seconds,
        sock_read=seconds,
        ceil_threshold=math.inf,
    )


def _network_kind(error: aiohttp.ClientError | OSError) -> str:
    if isinstance(error, (aiohttp.ClientSSLError, aiohttp.ServerFingerprintMismatch, ssl.SSLError)):
        return "tls"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, aiohttp.ClientConnectorDNSError):
        return "dns"
    underlying = error.os_error if isinstance(error, aiohttp.ClientConnectorError) else error
    if isinstance(underlying, socket.gaierror):
        return "dns"
    if isinstance(underlying, OSError) and (
        isinstance(underlying, TimeoutError)
        or underlying.errno in {errno.ETIMEDOUT, 10060}
        or getattr(underlying, "winerror", None) == 10060
    ):
        return "timeout"
    if isinstance(underlying, OSError) and (
        underlying.errno in _NO_ROUTE or getattr(underlying, "winerror", None) in _NO_ROUTE
    ):
        return "unavailable"
    if isinstance(error, aiohttp.ClientResponseError):
        return "http"
    return "connection"


async def _no_replay(
    request: aiohttp.ClientRequest, handler: ClientHandlerType
) -> aiohttp.ClientResponse:
    """Srun uses GET for mutations: prevent aiohttp's automatic GET replay.

    Middleware runs inside aiohttp's retry boundary. Convert its retryable
    connection errors before they escape that boundary, using only public APIs.
    """
    try:
        return await handler(request)
    except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError) as error:
        raise NetworkError(_network_kind(error)) from None


class SessionPool:
    """Separate authentication and traffic resources; reset only after quiescing tasks."""

    def __init__(self, families: Iterable[IPFamily]) -> None:
        self._families = tuple(dict.fromkeys(families))
        if any(family not in _FAMILIES for family in self._families):
            raise ProtocolError("invalid_input") from None
        self.auth: dict[IPFamily, aiohttp.ClientSession] = {}
        self.traffic: dict[IPFamily, aiohttp.ClientSession] = {}

    async def __aenter__(self) -> Self:
        await self.reset()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def _new_session(self, family: IPFamily, *, traffic: bool) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(
            family=_FAMILIES[family], ssl=True, limit=32 if traffic else 4
        )
        try:
            return aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar() if traffic else aiohttp.CookieJar(),
                headers={"Accept-Encoding": "identity"},
                # Reject encoded auth replies too: bound memory before decompression.
                auto_decompress=False,
                read_bufsize=_READ_BUFFER,
                timeout=(
                    aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=None)
                    if traffic
                    else _timeout(5)
                ),
                middlewares=() if traffic else (_no_replay,),
            )
        except BaseException:
            await connector.close()
            raise

    async def reset(self) -> None:
        """Close old connectors/cookies/DNS caches and update the same dictionaries."""
        await self.close()
        try:
            for family in self._families:
                self.auth[family] = await self._new_session(family, traffic=False)
                self.traffic[family] = await self._new_session(family, traffic=True)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        sessions = (*self.auth.values(), *self.traffic.values())
        self.auth.clear()
        self.traffic.clear()
        # Finish every close even if the caller cancels during connector cleanup.
        closing = asyncio.gather(*(session.close() for session in sessions))
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise


def _valid_ac_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,16}", value)) and int(value) > 0


def _url(value: str, *, base: bool = False) -> URL:
    try:
        parsed = URL(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.user is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query_string
            or (base and parsed.path not in {"", "/"})
        ):
            raise ValueError
        # Validate ports at construction, before aiohttp can embed a URL in errors.
        _ = parsed.port
        return parsed
    except (ValueError, UnicodeError):
        raise ProtocolError("invalid_input") from None


def _decode(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeError:
        raise ProtocolError("invalid_response") from None


def _portal_ip(body: bytes) -> str:
    candidates = set()
    for match in _PORTAL_IP.finditer(_decode(body)):
        value = match["ip"]
        try:
            if "%" in value:
                continue
            ipaddress.ip_address(value)
        except ValueError:
            continue
        candidates.add(value)
    if len(candidates) != 1:
        raise ProtocolError("invalid_response") from None
    return candidates.pop()


def _status_username(value: object) -> str:
    if not isinstance(value, str) or any(ord(char) == 127 for char in value):
        raise ProtocolError("invalid_response") from None
    try:
        # Share the protocol's length, Unicode and whitespace validation.
        build_challenge_params(value)
    except ProtocolError:
        raise ProtocolError("invalid_response") from None
    return value


async def _read_bounded(response: aiohttp.ClientResponse) -> bytes:
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise ProtocolError("invalid_response") from None
    if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
        raise ProtocolError("response_too_large") from None
    body = bytearray()
    while True:
        chunk = await response.content.read(min(8192, MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProtocolError("response_too_large") from None


class SrunClient:
    def __init__(
        self,
        family: IPFamily,
        session_provider: Callable[[], aiohttp.ClientSession],
        *,
        ac_id: str | None = None,
        base_url: str | None = None,
        discovery_url: str | None = None,
        timeout_seconds: float = 5,
    ) -> None:
        if (
            family not in _FAMILIES
            or (ac_id is not None and not _valid_ac_id(ac_id))
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ProtocolError("invalid_input") from None
        self.family = family
        self._provider = session_provider
        self._ac_id = ac_id
        self._base = _url(base_url if base_url is not None else _BASE_URLS[family], base=True)
        self._discovery = _url(
            discovery_url if discovery_url is not None else _DISCOVERY_URLS[family]
        )
        self._timeout = _timeout(timeout_seconds)
        # ssl=True alone can inherit an injected connector's unverified context.
        self._tls = ssl.create_default_context()

    async def _send(
        self,
        session: aiohttp.ClientSession,
        url: URL,
        params: Mapping[str, str] | None,
        *,
        discovery: bool,
    ) -> tuple[bytes, str | None]:
        async with session.get(
            url,
            params=params,
            allow_redirects=False,
            timeout=self._timeout,
            ssl=self._tls,
            auto_decompress=False,
            read_bufsize=_READ_BUFFER,
            headers={"Accept-Encoding": "identity"},
            raise_for_status=False,
            middlewares=(_no_replay,),
        ) as response:
            try:
                if discovery and response.status in _REDIRECTS:
                    location = response.headers.get("Location")
                    if location:
                        # Location is data only. Never follow it, even on the same host.
                        return b"", location
                elif not 200 <= response.status < 300:
                    raise NetworkError("http") from None
                return await _read_bounded(response), None
            finally:
                # Do not drain large, partial, rejected or cancelled responses.
                response.close()

    async def _request(
        self,
        url: URL,
        params: Mapping[str, str] | None = None,
        *,
        anonymous: bool = False,
        discovery: bool = False,
    ) -> tuple[bytes, str | None]:
        # Fetch on EVERY request, including the request after a fresh challenge.
        # A broken provider is a programming error and must not be swallowed.
        session = self._provider()
        connector = session.connector
        if session.closed or connector is None:
            raise RuntimeError("Authentication session is closed") from None
        if (
            not isinstance(connector, aiohttp.TCPConnector)
            or connector.family != _FAMILIES[self.family]
            or session.trust_env
        ):
            raise ProtocolError("invalid_input") from None
        try:
            literal = ipaddress.ip_address(url.host or "")
        except ValueError:
            pass  # Hostnames must be resolved through the family-bound connector.
        else:
            if literal.version != (4 if self.family == "ipv4" else 6):
                # An incompatible target is not evidence that the local stack is absent.
                raise NetworkError("connection") from None
        try:
            if anonymous:
                # Share only the family-bound connector, never auth defaults/cookies.
                async with aiohttp.ClientSession(
                    connector=connector,
                    connector_owner=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    trust_env=False,
                    auto_decompress=False,
                    read_bufsize=_READ_BUFFER,
                ) as clean:
                    return await self._send(clean, url, params, discovery=discovery)
            return await self._send(session, url, params, discovery=discovery)
        except aiohttp.InvalidURL:
            raise ProtocolError("invalid_input") from None
        except (aiohttp.ClientError, OSError) as error:
            raise NetworkError(_network_kind(error)) from None

    def _check_access(self, access: AccessContext) -> None:
        if access.family != self.family or not _valid_ac_id(access.ac_id):
            raise ProtocolError("invalid_input") from None
        # Validate without substituting or normalizing the signing address.
        build_challenge_params("context-validation", access.ip)

    async def discover(self) -> AccessContext:
        if self._ac_id is not None:
            return AccessContext(self.family, self._ac_id)
        body, location = await self._request(self._discovery, anonymous=True, discovery=True)
        source = html.unescape(location if location is not None else _decode(body))
        candidates = {match[1] for match in _AC_ID.finditer(source) if _valid_ac_id(match[1])}
        if len(candidates) != 1:
            raise ProtocolError("invalid_response") from None
        return AccessContext(self.family, candidates.pop())

    async def status(self, access: AccessContext | None = None) -> OnlineObservation:
        # Read-only status must work when an online discovery target no longer
        # redirects to the portal. Go permits an empty ac_id for this query.
        if access is not None:
            self._check_access(access)
            ac_id = access.ac_id
        else:
            ac_id = self._ac_id or ""
        portal, _ = await self._request(self._base / "srun_portal_pc", {"ac_id": ac_id})
        ip = _portal_ip(portal)
        body, _ = await self._request(
            self._base / "cgi-bin/rad_user_info", {"ip": ip, "callback": CALLBACK}
        )
        data = parse_jsonp(body, CALLBACK)
        error = data.get("error")
        if not isinstance(error, str) or not error:
            raise ProtocolError("invalid_response") from None
        if error == "ok":
            username = _status_username(data.get("user_name"))
            code = data.get("ecode")
            if "ecode" not in data or code == "0" or (type(code) is int and code == 0):
                return OnlineObservation(self.family, OnlineState.ONLINE, username, ip, "online")
        # No translations, substring heuristics, or other schools' error-code tables.
        elif error == "not_online" and data.get("user_name") in (None, ""):
            return OnlineObservation(self.family, OnlineState.OFFLINE, ip=ip, reason="not_online")
        return OnlineObservation(
            self.family, OnlineState.UNKNOWN, ip=ip, reason="unrecognized_status"
        )

    async def login(self, credentials: Credentials, access: AccessContext) -> AuthReply:
        self._check_access(access)
        body, _ = await self._request(
            self._base / "cgi-bin/get_challenge",
            {**build_challenge_params(credentials.username, access.ip), "callback": CALLBACK},
        )
        challenge = parse_challenge(body, CALLBACK)
        params = build_login_params(credentials, challenge.token, ac_id=access.ac_id, ip=access.ip)
        reply, _ = await self._request(
            self._base / "cgi-bin/srun_portal", {**params, "callback": CALLBACK}
        )
        return parse_auth_reply(reply, CALLBACK)

    async def logout(self, username: str, access: AccessContext) -> AuthReply:
        self._check_access(access)
        body, _ = await self._request(
            self._base / "cgi-bin/get_challenge",
            {**build_challenge_params(username, access.ip), "callback": CALLBACK},
        )
        challenge = parse_challenge(body, CALLBACK)
        params = build_logout_params(username, challenge.token, ac_id=access.ac_id, ip=access.ip)
        reply, _ = await self._request(
            self._base / "cgi-bin/srun_portal", {**params, "callback": CALLBACK}
        )
        return parse_auth_reply(reply, CALLBACK)

    async def diagnose(self) -> dict[str, str]:
        """Read-only checks; values are fixed classifications, never remote text.

        DNS uses a separate, family-specific getaddrinfo with its own deadline;
        cached HTTP connections cannot determine that result. A lookup failure
        does not establish that the local IPv6 stack is unavailable. The timeout
        bounds our await; the OS resolver running in the executor is not killed.
        TLS is verified only for a successful HTTPS read; HTTP reports unknown.
        """
        results = {"dns": "unknown", "portal": "unknown", "tls": "unknown", "ac_id": "unknown"}
        try:
            async with asyncio.timeout(self._timeout.total):
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    self._base.host,
                    self._base.port,
                    family=_FAMILIES[self.family],
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            results["dns"] = (
                "ok"
                if any(address[0] == _FAMILIES[self.family] for address in addresses)
                else "failed"
            )
        except socket.gaierror:
            results["dns"] = "failed"
        except OSError as error:
            kind = _network_kind(error)
            results["dns"] = kind if kind in {"timeout", "unavailable"} else "failed"
        access = None
        try:
            access = await self.discover()
            results["ac_id"] = "configured" if self._ac_id is not None else "ok"
        except NetworkError as error:
            results["ac_id"] = error.kind
        except ProtocolError:
            results["ac_id"] = "protocol"
        try:
            await self._request(
                self._base / "srun_portal_pc",
                {"ac_id": access.ac_id} if access is not None else None,
                anonymous=True,
            )
            results["portal"] = "ok"
            if self._base.scheme == "https":
                results["tls"] = "ok"
        except NetworkError as error:
            results["portal"] = error.kind
            if error.kind == "tls":
                results["tls"] = "failed"
        except ProtocolError:
            results["portal"] = "protocol"
        return results
