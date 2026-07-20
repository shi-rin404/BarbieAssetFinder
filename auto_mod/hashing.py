"""XXH64 implementation for Auto Mod object identifiers."""

from __future__ import annotations


MASK64 = 0xFFFFFFFFFFFFFFFF
PRIME64_1 = 0x9E3779B185EBCA87
PRIME64_2 = 0xC2B2AE3D27D4EB4F
PRIME64_3 = 0x165667B19E3779F9
PRIME64_4 = 0x85EBCA77C2B2AE63
PRIME64_5 = 0x27D4EB2F165667C5


def rol64(value: int, count: int) -> int:
    value &= MASK64
    return ((value << count) | (value >> (64 - count))) & MASK64


def _round(accumulator: int, lane: int) -> int:
    accumulator = (accumulator + lane * PRIME64_2) & MASK64
    accumulator = rol64(accumulator, 31)
    accumulator = (accumulator * PRIME64_1) & MASK64
    return accumulator


def _merge_round(accumulator: int, lane: int) -> int:
    accumulator ^= _round(0, lane)
    accumulator = (accumulator * PRIME64_1 + PRIME64_4) & MASK64
    return accumulator


def xxh64(data: bytes, seed: int = 0) -> int:
    length = len(data)
    index = 0
    seed &= MASK64

    if length >= 32:
        v1 = (seed + PRIME64_1 + PRIME64_2) & MASK64
        v2 = (seed + PRIME64_2) & MASK64
        v3 = seed
        v4 = (seed - PRIME64_1) & MASK64
        limit = length - 32
        while index <= limit:
            v1 = _round(v1, int.from_bytes(data[index : index + 8], "little"))
            index += 8
            v2 = _round(v2, int.from_bytes(data[index : index + 8], "little"))
            index += 8
            v3 = _round(v3, int.from_bytes(data[index : index + 8], "little"))
            index += 8
            v4 = _round(v4, int.from_bytes(data[index : index + 8], "little"))
            index += 8

        result = (
            rol64(v1, 1)
            + rol64(v2, 7)
            + rol64(v3, 12)
            + rol64(v4, 18)
        ) & MASK64
        result = _merge_round(result, v1)
        result = _merge_round(result, v2)
        result = _merge_round(result, v3)
        result = _merge_round(result, v4)
    else:
        result = (seed + PRIME64_5) & MASK64

    result = (result + length) & MASK64

    while index + 8 <= length:
        lane = _round(0, int.from_bytes(data[index : index + 8], "little"))
        result ^= lane
        result = (rol64(result, 27) * PRIME64_1 + PRIME64_4) & MASK64
        index += 8

    if index + 4 <= length:
        result ^= int.from_bytes(data[index : index + 4], "little") * PRIME64_1
        result = (rol64(result, 23) * PRIME64_2 + PRIME64_3) & MASK64
        index += 4

    while index < length:
        result ^= data[index] * PRIME64_5
        result = (rol64(result, 11) * PRIME64_1) & MASK64
        index += 1

    result ^= result >> 33
    result = (result * PRIME64_2) & MASK64
    result ^= result >> 29
    result = (result * PRIME64_3) & MASK64
    result ^= result >> 32
    return result & MASK64


def object_id_for_name(name: str, existing_ids: set[str]) -> str:
    primary = f"{xxh64(name.encode('utf-8'), 163):016x}"
    if primary not in existing_ids:
        return primary
    return f"{xxh64(name.encode('utf-8'), 123):016x}"
