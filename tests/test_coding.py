"""GPLv3 GoAuthing vectors and additional synthetic byte-boundary checks.

Vectors: references/GoAuthing/libauth/coding_test.go at upstream commit
3730e252ca11e6abbd055d7850c417c5751c1efa. Other tests check properties;
they do not claim to supply new Go-generated expected ciphertexts.
"""

import base64
import struct
from collections.abc import Callable
from typing import cast

import pytest

from thuauth.errors import ProtocolError
from thuauth.protocol.coding import srbx1_encode, xencode

_TOKEN = b"aa0edd0fff7dd9f1f0ae4e981ec0114c7b0bf6f67c4895bed4f4ac634e97ecf2"
_ALPHABET = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
_STANDARD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _decode_srbx1(encoded: str) -> bytes:
    return base64.b64decode(
        encoded.translate(str.maketrans(_ALPHABET, _STANDARD_ALPHABET)), validate=True
    )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"1", "9+=="),
        (b"2", "9S=="),
        (b"34", "9z+="),
        (b"567", "0FZ7"),
        (b"\x00", "LL=="),
        (b"\x00\x00", "LLL="),
        (b"\xff\x00\x00", "AvLL"),
        (b"\x01", "L+=="),
        (b"\x01==!@#$%^&*()", "LFt52HLkRourRX//8+=="),
        (b"\x01aAbB_+=-\x11", "LaiVZYRs8ztfP+=="),
    ],
)
def test_srbx1_upstream_vectors(data: bytes, expected: str) -> None:
    assert srbx1_encode(data) == expected


@pytest.mark.parametrize(
    ("data", "key", "expected"),
    [
        (b"", _TOKEN, ""),
        (b"1", _TOKEN, "NmsaR0fCm5H="),
        (b"agfawegwq12834eqrge", _TOKEN, "DAxHygvRUjlDyJjmvChIzuavMsjy7B9L"),
        (b"agfawegwq12834eqrge", b"0" * 64, "TOdQ9ggF2y/mskS6Orkg+eUZIok9vqJr"),
        (b"9$02%8r89)(&22{}we[f]|s", _TOKEN, "kCG+xmvGAhCV717Y80Fk0o1YJ8SYvBdnUmQoqS=="),
    ],
)
def test_xencode_upstream_vectors(data: bytes, key: bytes, expected: str) -> None:
    encoded = xencode(data, key)
    # Decode the fixed upstream vector independently of our SRBX1 encoder.
    assert encoded == _decode_srbx1(expected)
    assert srbx1_encode(encoded) == expected
    assert type(encoded) is bytes


@pytest.mark.parametrize("key_length", range(18))
def test_empty_data_bypasses_key_length_validation(key_length: int) -> None:
    assert xencode(b"", b"k" * key_length) == b""


@pytest.mark.parametrize("key_length", range(13))
def test_nonempty_data_rejects_short_keys(key_length: int) -> None:
    with pytest.raises(ProtocolError) as caught:
        xencode(b"synthetic input", b"k" * key_length)
    assert caught.value.code == "invalid_input"
    assert str(caught.value) == str(ProtocolError("invalid_input"))


@pytest.mark.parametrize("key_length", [13, 14, 15, 16])
def test_key_partial_word_is_zero_padded(key_length: int) -> None:
    key = bytes(range(1, key_length + 1))
    assert xencode(b"synthetic input", key) == xencode(b"synthetic input", key.ljust(16, b"\0"))


def test_key_uses_only_first_four_words() -> None:
    data = b"synthetic input"
    prefix = _TOKEN[:16]
    assert xencode(data, prefix) == xencode(data, _TOKEN)
    assert xencode(data, prefix) == xencode(data, prefix + bytes(range(256)))
    assert xencode(data, prefix) != xencode(data, prefix[:15] + b"!")


def test_token_is_literal_bytes_not_hex_decoded() -> None:
    data = b"agfawegwq12834eqrge"
    assert xencode(data, _TOKEN) != xencode(data, bytes.fromhex(_TOKEN.decode("ascii")))
    assert xencode(data, b"0" * 64) != xencode(data, b"\0" * 32)


def _decrypt_words(ciphertext: bytes, key: bytes) -> tuple[int, ...]:
    """Invert the transform for layout/overflow checks, not a Go vector oracle.

    Struct supplies independent little-endian packing. Subtraction modulo 2**32
    reverses rounds, so boundary tests can inspect padding and the length word.
    """
    count = len(ciphertext) // 4
    values = list(struct.unpack(f"<{count}I", ciphertext))
    key_words = struct.unpack("<4I", key[:16].ljust(16, b"\0"))
    modulus = 2**32
    delta = 0x9E3779B9
    rounds = 6 + 52 // count
    total = (rounds * delta) % modulus
    y = values[0]
    for _ in range(rounds):
        for position in reversed(range(count)):
            z = values[position - 1]
            mix = (
                ((z >> 5) ^ (y << 2))
                + ((y >> 3) ^ (z << 4) ^ (total ^ y))
                + (key_words[(position % 4) ^ ((total >> 2) % 4)] ^ z)
            )
            values[position] = (values[position] - mix) % modulus
            y = values[position]
        total = (total - delta) % modulus
    return tuple(values)


@pytest.mark.parametrize(
    "length", [1, 2, 3, 4, 5, 7, 8, 9, 12, 13, 15, 16, 17, 51, 52, 53, 255, 256, 257]
)
@pytest.mark.parametrize("key", [b"\xff" * 13, b"\xff" * 16, _TOKEN])
def test_word_boundaries_length_word_and_uint32_overflow(length: int, key: bytes) -> None:
    # High bits force overflow in the shifts, sums and word updates.
    data = b"\xff" * length
    encoded = xencode(data, key)
    assert type(encoded) is bytes
    assert len(encoded) == 4 * ((length + 3) // 4 + 1)
    words = _decrypt_words(encoded, key)
    assert words[-1] == length
    assert struct.pack(f"<{len(words) - 1}I", *words[:-1]) == data + b"\0" * (-length % 4)


@pytest.mark.parametrize(
    "data",
    [
        bytes(range(256)),
        b"\0\xff\x80\0\xfe\x81\0",
        "清华校园网🔑é".encode(),
        "e\u0301".encode(),
        "é".encode(),
    ],
    ids=["all-bytes", "embedded-nuls", "unicode", "decomposed", "composed"],
)
def test_binary_and_unicode_bytes_are_preserved(data: bytes) -> None:
    encoded = xencode(data, _TOKEN)
    words = _decrypt_words(encoded, _TOKEN)
    assert words[-1] == len(data)
    assert struct.pack(f"<{len(words) - 1}I", *words[:-1]) == data + b"\0" * (-len(data) % 4)
    text = srbx1_encode(data)
    assert type(text) is str
    assert _decode_srbx1(text) == data


def test_length_word_distinguishes_trailing_zero_bytes() -> None:
    assert len({xencode(b"a" + b"\0" * count, _TOKEN) for count in range(4)}) == 4


@pytest.mark.parametrize("length", [0, 1, 2, 3, 4, 5, 6, 255, 256, 257, 258])
def test_srbx1_padding_return_type_and_no_prefix(length: int) -> None:
    data = bytes(index % 256 for index in range(length))
    encoded = srbx1_encode(data)
    assert type(encoded) is str
    assert len(encoded) == 4 * ((length + 2) // 3)
    assert encoded.count("=") == -length % 3
    assert not encoded.startswith("{SRBX1}")
    assert _decode_srbx1(encoded) == data


def test_srbx1_all_alphabet_indices() -> None:
    # Pack each of the 64 six-bit indices once, independently of Base64 code.
    bits = "".join(f"{index:06b}" for index in range(64))
    data = int(bits, 2).to_bytes(48, "big")
    assert srbx1_encode(data) == _ALPHABET


class _MustNotCoerce:
    def __str__(self) -> str:
        raise AssertionError("invalid input must not be formatted")

    def __repr__(self) -> str:
        raise AssertionError("invalid input must not be represented")

    def __bytes__(self) -> bytes:
        raise AssertionError("invalid input must not be coerced")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "synthetic-secret",
        "",
        12,
        False,
        [],
        bytearray(b"secret"),
        bytearray(),
        memoryview(b"secret"),
        _MustNotCoerce(),
    ],
    ids=[
        "none",
        "text",
        "empty-text",
        "int",
        "bool",
        "list",
        "bytearray",
        "empty-bytearray",
        "memoryview",
        "unprintable",
    ],
)
@pytest.mark.parametrize(
    "operation",
    [
        lambda value: xencode(value, _TOKEN),
        lambda value: xencode(b"synthetic input", value),
        lambda value: xencode(b"", value),
        srbx1_encode,
    ],
    ids=["xencode-data", "xencode-key", "xencode-empty-data-key", "srbx1-data"],
)
def test_nonbytes_are_rejected_without_disclosure(
    value: object,
    operation: Callable[[bytes], object],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ProtocolError) as caught:
        operation(cast(bytes, value))
    assert caught.value.code == "invalid_input"
    assert caught.value.args == ProtocolError("invalid_input").args
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert not caplog.records
