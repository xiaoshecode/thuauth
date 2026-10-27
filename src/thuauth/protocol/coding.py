"""Byte-compatible Srun encodings, ported from z4yx/GoAuthing under GPLv3.

Source: references/GoAuthing/libauth/coding.go
Upstream commit: 3730e252ca11e6abbd055d7850c417c5751c1efa
Python changes: explicit bytes APIs and sanitized validation of invalid inputs.
"""

import base64

from thuauth.errors import ProtocolError

_UINT32_MASK = 0xFFFFFFFF
_DELTA = 0x9E3779B9
_SRBX1_TRANSLATION = bytes.maketrans(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
    b"LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA",
)


def xencode(data: bytes, key: bytes) -> bytes:
    """Encode raw bytes using the upstream uint32, little-endian algorithm.

    The key is used literally, without hex decoding. Nonempty data requires
    at least 13 key bytes (four words after zero padding); bytes after the
    first 16 are ignored. The encrypted length word remains in the output.
    Empty data returns empty bytes, even when the bytes key is short.
    """
    if not isinstance(data, bytes) or not isinstance(key, bytes):
        raise ProtocolError("invalid_input")
    if not data:
        return b""
    if len(key) < 13:
        raise ProtocolError("invalid_input")

    words = [
        int.from_bytes(data[offset : offset + 4], "little") for offset in range(0, len(data), 4)
    ]
    words.append(len(data) & _UINT32_MASK)
    key_words = [int.from_bytes(key[offset : offset + 4], "little") for offset in range(0, 16, 4)]

    last = len(words) - 1
    z = words[last]
    total = 0
    for _ in range(6 + 52 // len(words)):
        total = (total + _DELTA) & _UINT32_MASK
        e = (total >> 2) & 3
        for position in range(len(words)):
            y = words[0] if position == last else words[position + 1]
            mix = (z >> 5) ^ ((y << 2) & _UINT32_MASK)
            mix = (mix + ((y >> 3) ^ ((z << 4) & _UINT32_MASK) ^ (total ^ y))) & _UINT32_MASK
            mix = (mix + (key_words[(position & 3) ^ e] ^ z)) & _UINT32_MASK
            words[position] = (words[position] + mix) & _UINT32_MASK
            z = words[position]

    return b"".join(word.to_bytes(4, "little") for word in words)


def srbx1_encode(data: bytes) -> str:
    """Return custom-alphabet Base64 with padding, without the {SRBX1} prefix."""
    if not isinstance(data, bytes):
        raise ProtocolError("invalid_input")
    return base64.b64encode(data).translate(_SRBX1_TRANSLATION).decode("ascii")
