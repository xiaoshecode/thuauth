"""Compare Python with committed synthetic output from the pinned Go source.

Normal pytest runs require no Go installation; regenerate with tools/README.md.
"""

import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from thuauth.models import Credentials
from thuauth.protocol.coding import srbx1_encode, xencode
from thuauth.protocol.srun import (
    build_challenge_params,
    build_login_params,
    build_logout_params,
    serialize_info,
)

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/go_protocol_vectors.json").read_text(encoding="utf-8")
)


def test_fixtures_are_explicitly_synthetic_and_pinned() -> None:
    assert VECTORS["schema_version"] == 1
    assert VECTORS["synthetic"] is True
    assert VECTORS["source_commit"] == "3730e252ca11e6abbd055d7850c417c5751c1efa"
    assert len(VECTORS["requests"]) >= 8


@pytest.mark.parametrize("case", VECTORS["coding"], ids=lambda case: case["name"])
def test_coding_matches_actual_go_fixture(case) -> None:
    data = bytes.fromhex(case["input_hex"])
    key = bytes.fromhex(case["key_hex"])
    encoded = xencode(data, key)
    assert encoded.hex() == case["xencode_hex"]
    assert srbx1_encode(encoded) == case["srbx1"]
    assert srbx1_encode(data) == case["base64"]


@pytest.mark.parametrize("case", VECTORS["requests"], ids=lambda case: case["name"])
def test_complete_request_matches_actual_go_fixture(case) -> None:
    kwargs = {"token": case["token"], "ip": case["ip"], "ac_id": case["ac_id"]}
    if case["logout"]:
        params = build_logout_params(case["username"], **kwargs)
        password = None
        assert "password" not in params
    else:
        params = build_login_params(Credentials(case["username"], case["password"]), **kwargs)
        password = case["password"]
    raw = serialize_info(case["username"], password, ip=case["ip"], ac_id=case["ac_id"])
    assert raw == case["raw_info_json"].encode("utf-8")
    assert params == case["params"]
    assert urlencode(sorted(params.items())) == case["query"]
    assert build_challenge_params(case["username"], case["ip"]) == case["challenge_params"]
