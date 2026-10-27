"""One-shot, anonymous target diagnostics, independent of authentication state."""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Mapping

import aiohttp
from yarl import URL

from thuauth.config import Config
from thuauth.errors import NetworkError
from thuauth.http import _network_kind, _no_replay, _timeout
from thuauth.models import IPFamily

_BODY_LIMIT = 1024
_FAMILIES = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}


async def _diagnose_target(
    session: aiohttp.ClientSession, family: IPFamily, target_url: str, tls: ssl.SSLContext
) -> tuple[int | None, str]:
    connector = session.connector
    if session.closed or connector is None:
        raise RuntimeError("Diagnostic session is closed") from None
    if not isinstance(connector, aiohttp.TCPConnector) or connector.family != _FAMILIES[family]:
        raise RuntimeError("Diagnostic connector has an incompatible address family") from None
    try:
        url = URL(target_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.user is not None
            or url.fragment
        ):
            raise ValueError
        _ = url.port
    except ValueError:
        return None, "http"
    try:
        literal = ipaddress.ip_address(url.host)
    except ValueError:
        pass
    else:
        if literal.version != (4 if family == "ipv4" else 6):
            # An incompatible target is not evidence that the local stack is absent.
            return None, "connection"

    status = None
    try:
        # Borrow only the pool's connector. No inherited cookies, auth headers,
        # explicit proxies, environment proxies, netrc or trace logging.
        async with (
            aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                auto_decompress=False,
                read_bufsize=_BODY_LIMIT,
                timeout=_timeout(5),
                middlewares=(_no_replay,),
            ) as clean,
            clean.get(
                url,
                headers={"Range": "bytes=0-1023", "Accept-Encoding": "identity"},
                allow_redirects=False,
                ssl=tls,
            ) as response,
        ):
            try:
                status = response.status
                if 300 <= status < 400:
                    return status, "redirect"
                if not 200 <= status < 300:
                    return status, "http"
                remaining = _BODY_LIMIT
                while remaining:
                    chunk = await response.content.read(remaining)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                return status, "ok"
            finally:
                # Never drain an ignored Range, error body or cancelled response.
                response.close()
    except NetworkError as error:
        # The transport middleware prevents aiohttp replay before classifying errors.
        return status, error.kind
    except (aiohttp.ClientError, OSError) as error:
        return status, _network_kind(error)


async def diagnose_targets(
    config: Config, sessions: Mapping[IPFamily, aiohttp.ClientSession]
) -> list[dict[str, object]]:
    """Check each configured target once per applicable enabled address family.

    Pass SessionPool.traffic. The caller retains ownership of those sessions.
    Both probe and bulk lists are inspected even when bulk is disabled or has
    no workers. Order is probe then bulk, target index, then IPv4/IPv6.

    Each result contains kind (probe/bulk), zero-based index within that list,
    family, status (HTTP integer or None), and result. Result is one of ok,
    redirect, http, timeout, dns, unavailable, tls, connection. No URL, address,
    response body or exception text leaves this boundary. ok means the bounded
    request succeeded, not that authentication or sustained bulk is available.

    Read at most 1024 application body bytes, even if Range is ignored. This is
    not a bound on network/TLS overhead or transport prefetch. No redirects or
    retries, login, background workers or automatic recovery are performed.
    """
    families: list[IPFamily] = []
    if config.auth.ipv4:
        families.append("ipv4")
    if config.auth.ipv6:
        families.append("ipv6")
    tls = ssl.create_default_context()
    results: list[dict[str, object]] = []
    for kind, targets in (("probe", config.probe.targets), ("bulk", config.bulk.targets)):
        for index, target in enumerate(targets):
            for family in families:
                if target.family not in {family, "both"}:
                    continue
                status, result = await _diagnose_target(sessions[family], family, target.url, tls)
                results.append(
                    {
                        "kind": kind,
                        "index": index,
                        "family": family,
                        "status": status,
                        "result": result,
                    }
                )
    return results
