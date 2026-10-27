"""Pure request construction and bounded JSONP parsing for the Tsinghua Srun portal.

Ported from z4yx/GoAuthing libauth/requests.go and messages.go (GPL-3.0-only),
commit 3730e252ca11e6abbd055d7850c417c5751c1efa; see NOTICE.md.
Returned request mappings and serialized info contain secrets: do not log them.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from typing import cast

from thuauth.errors import ProtocolError
from thuauth.models import AuthReply, AuthReplyKind, Challenge, Credentials
from thuauth.protocol.coding import srbx1_encode, xencode

CALLBACK = "C_a_l_l_b_a_c_k"
MAX_RESPONSE_BYTES = 65536

# Only recognized codes/messages may leave the protocol boundary. Unknown response
# text may contain a password, token or full URL, even when it looks like a code.
_ERRORS = {
    "E2531": (AuthReplyKind.BLOCKED, "用户不存在"),
    "E2553": (AuthReplyKind.BLOCKED, "密码错误"),
    "E2606": (AuthReplyKind.BLOCKED, "用户被禁用"),
    "E2534": (AuthReplyKind.BLOCKED, "有代理行为被暂时禁用"),
    "E2611": (AuthReplyKind.BLOCKED, "MAC 绑定错误"),
    "E2612": (AuthReplyKind.BLOCKED, "MAC 在黑名单中"),
    "E2613": (AuthReplyKind.BLOCKED, "NAS PORT 绑定错误"),
    "E2614": (AuthReplyKind.BLOCKED, "VLAN ID 绑定错误"),
    "E2615": (AuthReplyKind.BLOCKED, "IP 绑定错误"),
    "E2616": (AuthReplyKind.BLOCKED, "已欠费"),
    "E3001": (AuthReplyKind.BLOCKED, "流量或时长已用尽"),
    "E3004": (AuthReplyKind.BLOCKED, "余额不足"),
    "E5992": (AuthReplyKind.BLOCKED, "找不到这个用户"),
    "E2532": (AuthReplyKind.THROTTLED, "两次认证的间隔太短"),
    "E2533": (AuthReplyKind.THROTTLED, "尝试次数过于频繁"),
    "E2620": (AuthReplyKind.ALREADY_ONLINE, "已经在线，需要复查在线账号和地址"),
}


def _text(value: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not value and not allow_empty):
        raise ProtocolError("invalid_input")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ProtocolError("invalid_input") from None
    return value


def _username(value: str) -> str:
    result = _text(value, 256)
    if any(char.isspace() or ord(char) < 32 for char in result):
        raise ProtocolError("invalid_input")
    return result


def _ip(value: str) -> str:
    result = _text(value, 45, allow_empty=True)
    if result:
        try:
            if "%" in result:
                raise ValueError
            ipaddress.ip_address(result)
        except ValueError:
            raise ProtocolError("invalid_input") from None
    # Retain the caller's exact spelling and empty-IP semantics for signing.
    return result


def _ac_id(value: str) -> str:
    result = _text(value, 16)
    if not re.fullmatch(r"[0-9]+", result) or int(result) == 0:
        raise ProtocolError("invalid_input")
    return result


def _token(value: str) -> bytes:
    result = _text(value, 4096).encode("utf-8")
    # Go consumes four little-endian words; 13 bytes supply the fourth padded word.
    if len(result) < 13:
        raise ProtocolError("invalid_input")
    return result


def serialize_info(username: str, password: str | None, *, ip: str, ac_id: str) -> bytes:
    """Match Go json.Marshal(map[string]string); None omits password for logout."""
    data = {
        "username": _username(username),
        "ip": _ip(ip),
        "acid": _ac_id(ac_id),
        "enc_ver": "srun_bx1",
    }
    if password is not None:
        data["password"] = _text(password, 4096, allow_empty=True)
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for char, escaped in (
        ("&", r"\u0026"),
        ("<", r"\u003c"),
        (">", r"\u003e"),
        ("\u2028", r"\u2028"),
        ("\u2029", r"\u2029"),
    ):
        text = text.replace(char, escaped)
    return text.encode("utf-8")


def build_challenge_params(username: str, ip: str = "") -> dict[str, str]:
    return {"username": _username(username), "ip": _ip(ip), "double_stack": "1"}


def _build_params(
    username: str, password: str | None, token: str, *, ip: str, ac_id: str
) -> dict[str, str]:
    key = _token(token)
    raw_info = serialize_info(username, password, ip=ip, ac_id=ac_id)
    info = "{SRBX1}" + srbx1_encode(xencode(raw_info, key))
    params = {
        "action": "logout" if password is None else "login",
        "ac_id": ac_id,
        "n": "200",
        "type": "1",
        "ip": ip,
        "double_stack": "1",
        "username": username,
        "info": info,
    }
    parts = [username]
    if password is not None:
        digest = hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()
        params["password"] = "{MD5}" + digest
        parts.append(digest)
    parts.extend((ac_id, ip, params["n"], params["type"], info))
    checksum = "".join(token + part for part in parts).encode("utf-8")
    params["chksum"] = hashlib.sha1(checksum, usedforsecurity=False).hexdigest()
    return params


def build_login_params(
    credentials: Credentials, token: str, *, ac_id: str, ip: str = ""
) -> dict[str, str]:
    """Build login fields without a callback, URL encoding, I/O or implicit IP changes."""
    if not isinstance(credentials, Credentials):
        raise ProtocolError("invalid_input")
    # Presence checks belong to Config/AuthService; the pure codec also models
    # the reference empty-password case without confusing it with logout (None).
    _text(credentials.password, 4096, allow_empty=True)
    return _build_params(credentials.username, credentials.password, token, ip=ip, ac_id=ac_id)


def build_logout_params(username: str, token: str, *, ac_id: str, ip: str = "") -> dict[str, str]:
    """Logout deliberately accepts no password and signs the password-free info."""
    return _build_params(username, None, token, ip=ip, ac_id=ac_id)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError
    return value


def _reject_constant(text: str) -> object:
    raise ValueError


def parse_jsonp(body: bytes, callback: str = CALLBACK) -> dict[str, object]:
    """Accept outer ASCII whitespace and one optional semicolon, never execute JS."""
    if not isinstance(callback, str) or not re.fullmatch(
        r"[A-Za-z_$][A-Za-z0-9_$]{0,63}", callback
    ):
        raise ProtocolError("invalid_callback")
    if not isinstance(body, bytes):
        raise ProtocolError("invalid_jsonp")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProtocolError("response_too_large")
    try:
        text = body.decode("utf-8").strip(" \t\r\n")
        if text.endswith(";"):
            text = text[:-1].rstrip(" \t\r\n")
        prefix = callback + "("
        if not text.startswith(prefix) or not text.endswith(")"):
            raise ValueError
        value = json.loads(
            text[len(prefix) : -1],
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
        if not isinstance(value, dict):
            raise ValueError
        return cast(dict[str, object], value)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_jsonp") from None


def parse_challenge(body: bytes, callback: str = CALLBACK) -> Challenge:
    data = parse_jsonp(body, callback)
    token = data.get("challenge")
    reported_ip = data.get("client_ip")
    if data.get("res") != "ok" or not isinstance(token, str):
        raise ProtocolError("invalid_response")
    try:
        _token(token)
        if reported_ip is not None:
            if not isinstance(reported_ip, str):
                raise ProtocolError("invalid_input")
            _ip(reported_ip)
    except ProtocolError:
        raise ProtocolError("invalid_response") from None
    # Record evidence only; the HTTP/auth layer must decide any IP binding separately.
    return Challenge(token, reported_ip or None)


def parse_auth_reply(body: bytes, callback: str = CALLBACK) -> AuthReply:
    data = parse_jsonp(body, callback)
    error = data.get("error")
    if not isinstance(error, str) or not error:
        raise ProtocolError("invalid_response")
    code = data.get("ecode")
    known_code = code if isinstance(code, str) and code in _ERRORS else None
    if error == "ok":
        # An explicit error code must be a zero success marker. Do not accept
        # malformed values or an unmapped error merely because error says "ok".
        if "ecode" in data and not (code == "0" or (type(code) is int and code == 0)):
            raise ProtocolError("invalid_response")
        return AuthReply(AuthReplyKind.ACCEPTED, None, "认证接口已接受操作；在线状态仍需复查")
    if known_code is not None:
        kind, message = _ERRORS[known_code]
        return AuthReply(kind, known_code, message)
    return AuthReply(AuthReplyKind.REJECTED, None, "认证接口拒绝操作，原因未识别")
