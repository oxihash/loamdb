"""Turns logical values into byte strings whose lexicographic (unsigned
byte-wise) order matches the value's own natural order. The B+tree only
ever compares raw bytes, so every value that needs correct ordering
(primary keys, ORDER BY columns) has to go through here first.
"""
from __future__ import annotations

_INT_BIAS = 1 << 63  # shifts the signed 64-bit range to unsigned so two's-complement bit patterns don't reorder negatives after the positives


def encode_int(n: int) -> bytes:
    if not (-(1 << 63) <= n < (1 << 63)):
        raise ValueError(f"integer {n} out of the supported signed 64-bit range")
    return (n + _INT_BIAS).to_bytes(8, "big")


def decode_int(data: bytes) -> int:
    return int.from_bytes(data, "big") - _INT_BIAS


def encode_str(s: str) -> bytes:
    # UTF-8 byte order matches Unicode codepoint order for every codepoint
    # actually used here (this doesn't hold in general for all of Unicode
    # once surrogate-pair edge cases are involved, which doesn't matter for
    # ordinary text).
    return s.encode("utf-8")


def decode_str(data: bytes) -> str:
    return data.decode("utf-8")
