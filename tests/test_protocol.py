"""All inputs are synthetic. No campus endpoints or real credentials are used."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from thuauth.errors import ProtocolError
from thuauth.models import AuthReplyKind, Credentials
from thuauth.protocol.srun import (
    CALLBACK,
    MAX_RESPONSE_BYTES,
    build_login_params,
    build_logout_params,
    parse_auth_reply,
    parse_challenge,
    parse_jsonp,
    serialize_info,
)

TOKEN = "synthetic-challenge-token-0123456789abcdef"
SECRET = "SyntheticPrivateValue123"


def _reply(value: object, callback: str = CALLBACK) -> bytes:
    return f"{callback}({json.dumps(value, ensure_ascii=False)})".encode()


def test_logout_has_no_password_or_password_digest_dependency() -> None:
    params = build_logout_params("synthetic-user", TOKEN, ip="", ac_id="01")
    assert params["action"] == "logout"
    assert "password" not in params
    assert "password" not in json.loads(serialize_info("synthetic-user", None, ip="", ac_id="01"))
    assert params["ac_id"] == "01"


@pytest.mark.parametrize(
    "overrides",
    [
        {"token": "short"},
        {"token": ""},
        {"token": "\ud800"},
        {"token": "x" * 4097},
        {"ip": "not-an-ip"},
        {"ip": "fe80::1%12"},
        {"ip": "001.2.3.4"},
        {"ip": "1.2.3.4\n"},
        {"ac_id": "0"},
        {"ac_id": "-1"},
        {"ac_id": "１"},
        {"ac_id": "1" * 17},
    ],
)
def test_invalid_signing_context_is_rejected_without_echo(overrides) -> None:
    kwargs = {"token": TOKEN, "ip": "", "ac_id": "1", **overrides}
    with pytest.raises(ProtocolError) as caught:
        build_login_params(Credentials("synthetic-user", SECRET), **kwargs)
    assert caught.value.code == "invalid_input"
    assert SECRET not in str(caught.value)
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "credentials",
    [
        Credentials("", SECRET),
        Credentials("synthetic user", SECRET),
        Credentials("x" * 257, SECRET),
        Credentials("synthetic-user", "\ud800"),
        Credentials("synthetic-user", "x" * 4097),
    ],
)
def test_invalid_credentials_never_enter_signing(credentials) -> None:
    with pytest.raises(ProtocolError) as caught:
        build_login_params(credentials, TOKEN, ac_id="1")
    assert caught.value.code == "invalid_input"
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("ip", ["", "192.0.2.1", "2001:0DB8:0000:0000:0000:0000:0000:0001"])
def test_ip_is_validated_without_changing_signed_spelling(ip: str) -> None:
    params = build_login_params(Credentials("synthetic-user", SECRET), TOKEN, ip=ip, ac_id="1")
    assert params["ip"] == ip
    assert json.loads(serialize_info("synthetic-user", SECRET, ip=ip, ac_id="1"))["ip"] == ip


@pytest.mark.parametrize("suffix", [b"", b";", b"; \n", b" \t;\r\n"])
def test_jsonp_allows_only_documented_outer_whitespace_and_semicolon(suffix) -> None:
    body = b" \r\n" + _reply({"error": "ok", "message": "literal );( text"}) + suffix
    assert parse_jsonp(body)["error"] == "ok"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html>login required</html>",
        b'wrong({"error":"ok"})',
        b'C_a_l_l_b_a_c_k({"error":"ok"}',
        b'C_a_l_l_b_a_c_k({"error":"ok"});evil()',
        b'C_a_l_l_b_a_c_k({"error":"ok"});;',
        b"C_a_l_l_b_a_c_k()",
        b"C_a_l_l_b_a_c_k([])",
        b"C_a_l_l_b_a_c_k(null)",
        b'C_a_l_l_b_a_c_k({"error":"ok","error":"no"})',
        b'C_a_l_l_b_a_c_k({"nested":{"x":1,"x":2}})',
        b'C_a_l_l_b_a_c_k({"value":NaN})',
        b'C_a_l_l_b_a_c_k({"value":Infinity})',
        b'C_a_l_l_b_a_c_k({"value":-Infinity})',
        b'C_a_l_l_b_a_c_k({"value":1e999})',
        b'C_a_l_l_b_a_c_k({"error":"\xff"})',
        b'\xef\xbb\xbfC_a_l_l_b_a_c_k({"error":"ok"})',
        b"C_a_l_l_b_a_c_k(" + b"[" * 1500 + b"0" + b"]" * 1500 + b")",
        b'C_a_l_l_b_a_c_k({"value":' + b"9" * 5000 + b"})",
        _reply(SECRET),
        _reply([SECRET]),
    ],
)
def test_bad_jsonp_never_echoes_untrusted_response(body) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_jsonp(body)
    assert caught.value.code == "invalid_jsonp"
    assert SECRET not in str(caught.value)
    assert "Traceback" not in str(caught.value)


@pytest.mark.parametrize("callback", ["", "x);evil(", "namespace.callback", "x" * 65, SECRET + ":"])
def test_invalid_expected_callback_is_not_echoed(callback) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_jsonp(_reply({}), callback)
    assert caught.value.code == "invalid_callback"
    assert SECRET not in str(caught.value)


def test_jsonp_size_bound_is_measured_before_decoding() -> None:
    prefix = CALLBACK.encode() + b'({"value":"'
    suffix = b'"})'
    body = prefix + b"x" * (MAX_RESPONSE_BYTES - len(prefix) - len(suffix)) + suffix
    assert parse_jsonp(body)["value"]
    with pytest.raises(ProtocolError) as caught:
        parse_jsonp(body + b" ")
    assert caught.value.code == "response_too_large"


def test_challenge_retains_but_does_not_apply_server_ip() -> None:
    challenge = parse_challenge(
        _reply({"res": "ok", "challenge": TOKEN, "client_ip": "2001:db8::2"})
    )
    assert challenge.token == TOKEN
    assert challenge.reported_ip == "2001:db8::2"
    assert TOKEN not in repr(challenge)
    assert "2001:db8::2" not in repr(challenge)
    with pytest.raises(FrozenInstanceError):
        challenge.token = "changed"
    params = build_login_params(Credentials("synthetic-user", SECRET), challenge.token, ac_id="1")
    assert params["ip"] == ""


@pytest.mark.parametrize("reported", [{}, {"client_ip": ""}, {"client_ip": None}])
def test_challenge_server_ip_is_optional(reported) -> None:
    assert (
        parse_challenge(_reply({"res": "ok", "challenge": TOKEN, **reported})).reported_ip is None
    )


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"res": "ok"},
        {"res": "failed", "challenge": TOKEN},
        {"res": "ok", "challenge": 123},
        {"res": "ok", "challenge": "short"},
        {"res": "ok", "challenge": TOKEN, "client_ip": SECRET},
        {"res": "ok", "challenge": TOKEN, "client_ip": 123},
        {"res": "ok", "challenge": TOKEN, "client_ip": "fe80::1%12"},
    ],
)
def test_bad_challenge_is_a_protocol_error_not_an_offline_result(response) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_challenge(_reply(response))
    assert caught.value.code == "invalid_response"
    assert TOKEN not in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("E2531", AuthReplyKind.BLOCKED),
        ("E2553", AuthReplyKind.BLOCKED),
        ("E2606", AuthReplyKind.BLOCKED),
        ("E2534", AuthReplyKind.BLOCKED),
        ("E2611", AuthReplyKind.BLOCKED),
        ("E2612", AuthReplyKind.BLOCKED),
        ("E2613", AuthReplyKind.BLOCKED),
        ("E2614", AuthReplyKind.BLOCKED),
        ("E2615", AuthReplyKind.BLOCKED),
        ("E2616", AuthReplyKind.BLOCKED),
        ("E3001", AuthReplyKind.BLOCKED),
        ("E3004", AuthReplyKind.BLOCKED),
        ("E5992", AuthReplyKind.BLOCKED),
        ("E2532", AuthReplyKind.THROTTLED),
        ("E2533", AuthReplyKind.THROTTLED),
        ("E2620", AuthReplyKind.ALREADY_ONLINE),
    ],
)
def test_known_auth_rejections_have_safe_categories(code, kind) -> None:
    reply = parse_auth_reply(_reply({"error": SECRET, "ecode": code, "message": TOKEN}))
    assert reply.kind is kind
    assert reply.code == code
    assert SECRET not in repr(reply)
    assert TOKEN not in repr(reply)


def test_success_means_accepted_not_verified_online() -> None:
    assert parse_auth_reply(_reply({"error": "ok"})).kind is AuthReplyKind.ACCEPTED
    assert parse_auth_reply(_reply({"error": "ok", "ecode": 0})).kind is AuthReplyKind.ACCEPTED
    assert parse_auth_reply(_reply({"error": "ok", "ecode": "0"})).kind is AuthReplyKind.ACCEPTED


@pytest.mark.parametrize(
    "ecode", ["E2553", "E3004", "E9999", 1, -1, 0.0, [], {}, None, "", False, True, SECRET]
)
def test_success_with_invalid_or_conflicting_error_code_is_rejected(ecode) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_auth_reply(_reply({"error": "ok", "ecode": ecode}))
    assert caught.value.code == "invalid_response"
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("ecode", [None, 123, [], {}, SECRET, "E9999", "E2553 " + SECRET])
def test_unknown_rejection_never_exposes_code_or_message(ecode) -> None:
    reply = parse_auth_reply(_reply({"error": SECRET, "ecode": ecode, "error_msg": TOKEN}))
    assert reply.kind is AuthReplyKind.REJECTED
    assert reply.code is None
    assert SECRET not in repr(reply)
    assert TOKEN not in repr(reply)


@pytest.mark.parametrize("data", [{}, {"error": None}, {"error": 0}, {"error": ""}, {"error": []}])
def test_missing_or_invalid_auth_outcome_is_a_protocol_error(data) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_auth_reply(_reply(data))
    assert caught.value.code == "invalid_response"


def test_unknown_protocol_error_code_cannot_echo_secrets() -> None:
    error = ProtocolError(SECRET)
    assert error.code == "invalid_response"
    assert SECRET not in repr(error)
