"""Literal THY asset lookup test helper.

This script intentionally bypasses path parsing and path normalization. The
given lookup path is encoded exactly as typed and hashed against the selected
archive THY table.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from filefinder.archive.idx_wpk import ArchiveIndexCache, extract_matching_entries
from filefinder.core.neox_xml import NEOX_BINARY_MAGIC, neox_bytes_to_text
from filefinder.core.paths import ArchiveSource, discover_archives, resolve_thy_path
from filefinder.lookup.flatbuffers import PathNotFoundError
from filefinder.lookup.thy import LookupResult, ThyLookupTable
from filefinder.lookup.xxhash import xxh32


NX_XML_EXTENSIONS = {".gim", ".mtg", ".mtl"}


@dataclass(frozen=True)
class LiteralLookup:
    raw_path: str
    hash128_hex: str
    used_fallback: bool
    primary_key: int
    final_key: int
    descriptor_index: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test THY asset lookup without path parsing or normalization."
    )
    parser.add_argument(
        "--game-root",
        type=Path,
        help="Game root directory. Defaults to user/memory.json game_root.",
    )
    parser.add_argument(
        "--archive",
        required=True,
        help="Archive stem or prefix, for example chr_player or chr/player.",
    )
    parser.add_argument(
        "--path",
        dest="flag_paths",
        action="append",
        default=[],
        help="Literal asset path to lookup. Can be passed multiple times.",
    )
    parser.add_argument(
        "--paths-file",
        type=Path,
        help="Text file containing one literal lookup path per line.",
    )
    parser.add_argument(
        "--compare-normalized",
        action="store_true",
        help="Also show the normal ThyLookupTable.lookup result for each path.",
    )
    parser.add_argument(
        "--verify-idx",
        action="store_true",
        help="Check whether the resolved Hash128 exists in the archive IDX/WPK files.",
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Write found payloads to tests/outputs/no_normalization using Hash128 filenames.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="When dumping, do not auto-decode NX-XML payloads.",
    )
    parser.add_argument("paths", nargs="*", help="Literal asset paths to lookup.")
    return parser.parse_args()


def resolve_game_root(game_root_arg: Path | None) -> Path:
    if game_root_arg is not None:
        return game_root_arg.resolve()

    memory_path = REPO_ROOT / "user" / "memory.json"
    try:
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        memory = {}
    stored = str(memory.get("game_root", "")).strip()
    if not stored:
        raise RuntimeError("No --game-root was passed and user/memory.json game_root is empty")
    return Path(stored).resolve()


def collect_paths(args: argparse.Namespace) -> list[str]:
    paths = list(args.flag_paths) + list(args.paths)
    if args.paths_file is not None:
        paths.extend(
            line.rstrip("\r\n")
            for line in args.paths_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    if not paths:
        raise ValueError("At least one lookup path is required")
    return paths


def select_archive(game_root: Path, archive_name: str) -> ArchiveSource:
    archives = discover_archives(game_root)
    if not archives:
        raise FileNotFoundError(
            f"No .idx archives were found in {game_root / 'res'} "
            f"or {game_root / 'Documents' / 'res'}"
        )

    requested = archive_name.strip().removesuffix(".idx")
    requested_prefix = requested.replace("\\", "/")
    for archive in archives.values():
        if archive.stem == requested or archive.prefix == requested_prefix:
            return archive

    available = ", ".join(sorted(archive.stem for archive in archives.values())[:20])
    suffix = " ..." if len(archives) > 20 else ""
    raise ValueError(f"Archive not found: {archive_name!r}. Available archive stems: {available}{suffix}")


def lookup_literal(table: ThyLookupTable, raw_path: str) -> LiteralLookup:
    encoded = raw_path.encode("utf-8")
    primary_seed = table.seeds[0]
    primary_key = xxh32(encoded, primary_seed)

    used_fallback = primary_key in table.collision_keys
    final_key = xxh32(encoded, table.seeds[1]) if used_fallback else primary_key
    descriptor_index = table.hash_to_descriptor_index.get(final_key)
    if descriptor_index is None:
        detail = ""
        if len(table.seeds) >= 2:
            alternate_key = xxh32(encoded, table.seeds[1])
            alternate_index = table.hash_to_descriptor_index.get(alternate_key)
            detail = f"; alternate_key=0x{alternate_key:08X}, alternate_index={alternate_index}"
        raise PathNotFoundError(
            f"Literal path is not present in THY table: {raw_path!r}; "
            f"primary=0x{primary_key:08X}, selected=0x{final_key:08X}{detail}"
        )

    descriptor = table.descriptor_vector + 4 + descriptor_index * table.descriptor_stride
    hash128 = table.reader.read(
        descriptor + table.descriptor_hash_offset,
        16,
        f"descriptor[{descriptor_index}].hash128",
    )
    return LiteralLookup(
        raw_path=raw_path,
        hash128_hex=hash128.hex(),
        used_fallback=used_fallback,
        primary_key=primary_key,
        final_key=final_key,
        descriptor_index=descriptor_index,
    )


def normalized_lookup(table: ThyLookupTable, raw_path: str) -> LookupResult | None:
    try:
        return table.lookup(raw_path)
    except Exception:
        return None


def maybe_decode_dump_data(data: bytes, raw_path: str, *, raw: bool) -> bytes:
    if raw:
        return data
    if Path(raw_path).suffix.lower() not in NX_XML_EXTENSIONS:
        return data
    if not data.startswith(NEOX_BINARY_MAGIC):
        return data
    return neox_bytes_to_text(data).encode("utf-8")


def verify_or_dump(
    archive: ArchiveSource,
    lookup: LiteralLookup,
    *,
    index_cache: ArchiveIndexCache,
    dump: bool,
    raw: bool,
) -> None:
    remaining = {lookup.hash128_hex}
    for idx_path in archive.idx_paths:
        found = extract_matching_entries(idx_path, remaining, index_cache=index_cache)
        entry = found.get(lookup.hash128_hex)
        if entry is None:
            continue

        print(f"  IDX/WPK: found in {idx_path}")
        if dump:
            output_root = REPO_ROOT / "tests" / "outputs" / "no_normalization" / archive.stem
            output_root.mkdir(parents=True, exist_ok=True)
            output_path = output_root / lookup.hash128_hex
            output_path.write_bytes(maybe_decode_dump_data(entry.data, lookup.raw_path, raw=raw))
            print(f"  Dumped: {output_path}")
        return
    print("  IDX/WPK: missing")


def main() -> int:
    args = parse_args()
    game_root = resolve_game_root(args.game_root)
    archive = select_archive(game_root, args.archive)
    table = ThyLookupTable(resolve_thy_path(game_root, archive.stem))
    paths = collect_paths(args)
    index_cache = ArchiveIndexCache()

    print(f"Game root: {game_root}")
    print(f"Archive: {archive.stem} ({archive.prefix})")
    print(f"THY: {table.thy_path}")
    print()

    failures = 0
    for raw_path in paths:
        print(f"Path: {raw_path!r}")
        try:
            literal = lookup_literal(table, raw_path)
        except Exception as exc:
            failures += 1
            print(f"  Literal lookup: failed ({exc})")
            print()
            continue

        print(f"  Hash128: {literal.hash128_hex}")
        print(f"  Primary key: 0x{literal.primary_key:08X}")
        print(f"  Final key: 0x{literal.final_key:08X}")
        print(f"  Descriptor index: {literal.descriptor_index}")
        print(f"  Used fallback seed: {literal.used_fallback}")

        if args.compare_normalized:
            normalized = normalized_lookup(table, raw_path)
            if normalized is None:
                print("  Normalized lookup: failed")
            else:
                status = "same" if normalized.hash128_hex == literal.hash128_hex else "different"
                print(f"  Normalized path: {normalized.normalized_path!r}")
                print(f"  Normalized Hash128: {normalized.hash128_hex} ({status})")

        if args.verify_idx or args.dump:
            verify_or_dump(
                archive,
                literal,
                index_cache=index_cache,
                dump=args.dump,
                raw=args.raw,
            )
        print()

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
