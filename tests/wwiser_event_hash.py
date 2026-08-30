from __future__ import annotations

import argparse


FNV_OFFSET_BASIS_32 = 0x811C9DC5
FNV_PRIME_32 = 0x01000193
UINT32_MASK = 0xFFFFFFFF


def wwise_short_id(name: str) -> int:
    """
    Wwise SoundBank/Event/Game Sync ShortID üretir.

    Algoritma:
    - Adı ASCII lowercase yap
    - 32-bit FNV-1 uygula
    """
    try:
        data = name.encode("ascii").lower()
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"Wwise nesne adı ASCII dışı karakter içeriyor: {name!r}"
        ) from exc

    hash_value = FNV_OFFSET_BASIS_32

    for byte in data:
        # FNV-1: önce çarpma, sonra XOR
        hash_value = (hash_value * FNV_PRIME_32) & UINT32_MASK
        hash_value ^= byte

    return hash_value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wwise Event/SoundBank/Game Sync ShortID hesaplar."
    )
    parser.add_argument(
        "names",
        nargs="+",
        help="Hashlenecek gerçek Wwise nesne adı",
    )
    args = parser.parse_args()

    for name in args.names:
        short_id = wwise_short_id(name)

        print(f"Name:    {name}")
        print(f"Decimal: {short_id}")
        print(f"Hex:     0x{short_id:08X}")
        print()


if __name__ == "__main__":
    main()