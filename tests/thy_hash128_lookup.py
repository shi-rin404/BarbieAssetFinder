from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from filefinder.core.paths import ParsedInput, discover_archives, parse_asset_path, resolve_thy_path
from filefinder.lookup.thy import LookupResult, ThyLookupTable


@dataclass(frozen=True)
class HashLookup:
    request: ParsedInput
    lookup: LookupResult
    thy_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Return the THY Hash128 value for one or more asset paths."
    )
    parser.add_argument(
        "--game-root",
        type=Path,
        help="Game root directory. Defaults to user/memory.json game_root.",
    )
    parser.add_argument(
        "--path",
        dest="flag_paths",
        action="append",
        default=[],
        help="Asset path to lookup. Can be passed multiple times.",
    )
    parser.add_argument(
        "--paths-file",
        type=Path,
        help="Text file containing one asset path per line.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output.",
    )
    parser.add_argument("paths", nargs="*", help="Asset paths to lookup.")
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
        raise ValueError("At least one asset path is required")
    return paths


def lookup_hashes(game_root: Path, raw_paths: list[str]) -> list[HashLookup]:
    archives = discover_archives(game_root)
    if not archives:
        raise FileNotFoundError(
            f"No .idx archives were found in {game_root / 'res'} "
            f"or {game_root / 'Documents' / 'res'}"
        )

    thy_tables: dict[str, tuple[Path, ThyLookupTable]] = {}
    results: list[HashLookup] = []
    for raw_path in raw_paths:
        request = parse_asset_path(raw_path, archives)
        cached = thy_tables.get(request.archive.stem)
        if cached is None:
            thy_path = resolve_thy_path(game_root, request.archive.stem)
            cached = (thy_path, ThyLookupTable(thy_path))
            thy_tables[request.archive.stem] = cached
        thy_path, table = cached
        results.append(
            HashLookup(
                request=request,
                lookup=table.lookup(request.normalized_path),
                thy_path=thy_path,
            )
        )
    return results


def print_text(results: list[HashLookup]) -> None:
    for item in results:
        print(f"Input: {item.request.raw_path}")
        print(f"Archive: {item.request.archive.stem}")
        print(f"Prefix: {item.request.archive.prefix}")
        print(f"Normalized path: {item.request.normalized_path}")
        print(f"THY: {item.thy_path}")
        print(f"Hash128: {item.lookup.hash128_hex}")
        print(f"Primary key: 0x{item.lookup.primary_key:08X}")
        print(f"Final key: 0x{item.lookup.final_key:08X}")
        print(f"Descriptor index: {item.lookup.descriptor_index}")
        print(f"Used fallback seed: {item.lookup.used_fallback}")
        print()


def print_json(results: list[HashLookup]) -> None:
    print(
        json.dumps(
            [
                {
                    "input": item.request.raw_path,
                    "archive": item.request.archive.stem,
                    "prefix": item.request.archive.prefix,
                    "normalized_path": item.request.normalized_path,
                    "thy_path": str(item.thy_path),
                    "hash128": item.lookup.hash128_hex,
                    "primary_key": f"0x{item.lookup.primary_key:08X}",
                    "final_key": f"0x{item.lookup.final_key:08X}",
                    "descriptor_index": item.lookup.descriptor_index,
                    "used_fallback_seed": item.lookup.used_fallback,
                }
                for item in results
            ],
            indent=4,
            ensure_ascii=False,
        )
    )


def main() -> int:
    args = parse_args()
    game_root = resolve_game_root(args.game_root)
    results = lookup_hashes(game_root, collect_paths(args))
    if args.json:
        print_json(results)
    else:
        print_text(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
