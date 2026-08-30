#!/usr/bin/env python3
"""Resolve a stored Hash128 value to its final XXH32 key in a THY/THFB table."""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import types
from pathlib import Path
from typing import Iterator, NamedTuple


class HashMatch(NamedTuple):
    descriptor_index: int
    final_xxh32: int
    stored_hash128: bytes


def load_thy_lookup_table():
    """
    Load thy.py together with its relative flatbuffers.py and xxhash.py imports.

    This bootstrap lets the script run directly when all four files are placed
    in the same directory, without requiring an __init__.py package file.
    """
    script_dir = Path(__file__).resolve().parent
    package_name = "_local_thy_lookup"

    package = types.ModuleType(package_name)
    package.__path__ = [str(script_dir)]
    package.__package__ = package_name
    sys.modules[package_name] = package

    for module_name in ("flatbuffers", "xxhash", "thy"):
        module_path = script_dir / f"{module_name}.py"
        if not module_path.is_file():
            raise FileNotFoundError(
                f"Required module is missing: {module_path}\n"
                "Place hash128_to_xxh32.py beside flatbuffers.py, thy.py, and xxhash.py."
            )

        qualified_name = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(qualified_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create an import spec for {module_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)

    return sys.modules[f"{package_name}.thy"].ThyLookupTable


def parse_int(value: str) -> int:
    """Parse decimal or 0x-prefixed integers."""
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid integer {value!r}; use decimal or 0x-prefixed hexadecimal."
        ) from exc


def parse_hash128(value: str) -> bytes:
    """
    Parse a 128-bit hash in file byte order.

    Accepted separators: spaces, tabs, newlines, colons, dashes, and underscores.
    """
    cleaned = value.strip()
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]

    cleaned = re.sub(r"[\s:_-]+", "", cleaned)
    if len(cleaned) != 32:
        raise ValueError(
            "Hash128 must contain exactly 32 hexadecimal digits "
            f"(16 bytes); received {len(cleaned)} digits."
        )

    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError("Hash128 contains non-hexadecimal characters.") from exc


def build_index_to_final_hash(table) -> list[int]:
    """
    Invert ThyLookupTable.hash_to_descriptor_index.

    The THY parser establishes that each final Hash32 vector entry corresponds
    to the descriptor at the same index.
    """
    result: list[int | None] = [None] * table.descriptor_count

    for final_hash32, descriptor_index in table.hash_to_descriptor_index.items():
        if not 0 <= descriptor_index < table.descriptor_count:
            raise RuntimeError(
                f"Descriptor index is outside the parsed table: {descriptor_index}"
            )
        if result[descriptor_index] is not None:
            raise RuntimeError(
                f"More than one final Hash32 maps to descriptor {descriptor_index}"
            )
        result[descriptor_index] = final_hash32

    missing = [index for index, value in enumerate(result) if value is None]
    if missing:
        preview = ", ".join(map(str, missing[:8]))
        suffix = "..." if len(missing) > 8 else ""
        raise RuntimeError(
            f"No final Hash32 was mapped to descriptor index(es): {preview}{suffix}"
        )

    return [value for value in result if value is not None]


def find_hash128_matches(table, target_hash128: bytes) -> Iterator[HashMatch]:
    """Yield all descriptors whose stored 16-byte Hash128 equals the target."""
    index_to_final_hash = build_index_to_final_hash(table)

    for descriptor_index, final_hash32 in enumerate(index_to_final_hash):
        descriptor = (
            table.descriptor_vector
            + 4
            + descriptor_index * table.descriptor_stride
        )
        stored_hash128 = table.reader.read(
            descriptor + table.descriptor_hash_offset,
            16,
            f"descriptor[{descriptor_index}].hash128",
        )

        if stored_hash128 == target_hash128:
            yield HashMatch(
                descriptor_index=descriptor_index,
                final_xxh32=final_hash32,
                stored_hash128=stored_hash128,
            )


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find the final XXH32 key associated with a stored Hash128 "
            "descriptor in a THY/THFB lookup table."
        )
    )
    parser.add_argument(
        "thy_file",
        type=Path,
        help="Path to the THY/THFB lookup table.",
    )
    parser.add_argument(
        "hash128",
        help="Known 128-bit hash as 32 hexadecimal digits.",
    )
    parser.add_argument(
        "--payload-xxh32-seed",
        type=parse_int,
        default=0x163F,
        help=(
            "Seed used only to validate the THFB payload checksum "
            "(default: 0x163F)."
        ),
    )
    parser.add_argument(
        "--skip-payload-validation",
        action="store_true",
        help="Parse the table even when its stored payload checksum does not match.",
    )
    return parser


def main() -> int:
    args = create_argument_parser().parse_args()

    try:
        target_hash128 = parse_hash128(args.hash128)
        ThyLookupTable = load_thy_lookup_table()
        table = ThyLookupTable(
            args.thy_file,
            validate_xxh32=not args.skip_payload_validation,
            xxh32_seed=args.payload_xxh32_seed,
        )
        matches = list(find_hash128_matches(table, target_hash128))
    except (OSError, ImportError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not matches:
        print(
            f"Hash128 {target_hash128.hex()} was not found in "
            f"{args.thy_file}.",
            file=sys.stderr,
        )
        return 2

    print(f"THY file         : {args.thy_file}")
    print(f"Variant          : {table.variant_name}")
    print(f"Hash128          : {target_hash128.hex()}")
    print(f"Match count      : {len(matches)}")

    for match_number, match in enumerate(matches, start=1):
        if len(matches) > 1:
            print(f"\nMatch {match_number}")
        print(f"Descriptor index : {match.descriptor_index}")
        print(f"XXH32 hex        : 0x{match.final_xxh32:08X}")
        print(f"XXH32 decimal    : {match.final_xxh32}")

    if len(matches) > 1:
        print(
            "\nWarning: The same Hash128 appears in multiple descriptors.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
