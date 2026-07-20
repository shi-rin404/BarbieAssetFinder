"""Pure Python XXH32 implementation used by NeoX asset lookup tables."""

MASK32 = 0xFFFFFFFF
PRIME32_1 = 0x9E3779B1
PRIME32_2 = 0x85EBCA77
PRIME32_3 = 0xC2B2AE3D
PRIME32_4 = 0x27D4EB2F
PRIME32_5 = 0x165667B1


def rol32(value: int, count: int) -> int:
    value &= MASK32
    return ((value << count) | (value >> (32 - count))) & MASK32


def xxh32(data: bytes, seed: int = 0) -> int:
    """Return the seeded XXH32 value used by the game runtime."""
    length = len(data)
    pos = 0
    seed &= MASK32

    if length >= 16:
        v1 = (seed + PRIME32_1 + PRIME32_2) & MASK32
        v2 = (seed + PRIME32_2) & MASK32
        v3 = seed
        v4 = (seed - PRIME32_1) & MASK32

        limit = length - 16
        while pos <= limit:
            lane1 = int.from_bytes(data[pos : pos + 4], "little")
            lane2 = int.from_bytes(data[pos + 4 : pos + 8], "little")
            lane3 = int.from_bytes(data[pos + 8 : pos + 12], "little")
            lane4 = int.from_bytes(data[pos + 12 : pos + 16], "little")
            pos += 16

            v1 = rol32((v1 + lane1 * PRIME32_2) & MASK32, 13)
            v1 = (v1 * PRIME32_1) & MASK32
            v2 = rol32((v2 + lane2 * PRIME32_2) & MASK32, 13)
            v2 = (v2 * PRIME32_1) & MASK32
            v3 = rol32((v3 + lane3 * PRIME32_2) & MASK32, 13)
            v3 = (v3 * PRIME32_1) & MASK32
            v4 = rol32((v4 + lane4 * PRIME32_2) & MASK32, 13)
            v4 = (v4 * PRIME32_1) & MASK32

        result = (
            rol32(v1, 1)
            + rol32(v2, 7)
            + rol32(v3, 12)
            + rol32(v4, 18)
        ) & MASK32
    else:
        result = (seed + PRIME32_5) & MASK32

    result = (result + length) & MASK32

    while pos + 4 <= length:
        lane = int.from_bytes(data[pos : pos + 4], "little")
        pos += 4
        result = (result + lane * PRIME32_3) & MASK32
        result = rol32(result, 17)
        result = (result * PRIME32_4) & MASK32

    while pos < length:
        result = (result + data[pos] * PRIME32_5) & MASK32
        pos += 1
        result = rol32(result, 11)
        result = (result * PRIME32_1) & MASK32

    result ^= result >> 15
    result = (result * PRIME32_2) & MASK32
    result ^= result >> 13
    result = (result * PRIME32_3) & MASK32
    result ^= result >> 16
    return result & MASK32

