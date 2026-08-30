from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from filefinder.core.neox_bpse import loads


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a BPSE binary file to JSON.")
    parser.add_argument("input", type=Path, help="BPSE binary input file.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="JSON output path. Defaults to the input path with .json suffix.",
    )
    parser.add_argument(
        "--root-only",
        action="store_true",
        help="Export only the BPSE root payload without the header wrapper.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Write compact JSON instead of pretty JSON.",
    )
    parser.add_argument(
        "--key-name",
        action="append",
        default=[],
        metavar="INDEX=NAME",
        help="Map a BPSE dictionary key index to a JSON key name. Can be repeated.",
    )
    parser.add_argument(
        "--raw-keys",
        action="store_true",
        help="Do not resolve NeoX BPSE root string table keys.",
    )
    return parser.parse_args()


def parse_key_names(values: list[str]) -> dict[int, str]:
    key_names: dict[int, str] = {}
    for value in values:
        index_text, separator, name = value.partition("=")
        if not separator or not index_text.isdecimal() or not name:
            raise ValueError(f"Invalid --key-name value: {value!r}. Expected INDEX=NAME.")
        key_names[int(index_text)] = name
    return key_names


def main() -> int:
    args = parse_args()
    input_path = args.input
    output_path = args.output or input_path.with_suffix(input_path.suffix + ".json")
    key_names = parse_key_names(args.key_name)

    data = input_path.read_bytes()
    text = loads(
        data,
        key_names=key_names or None,
        include_header=not args.root_only,
        indent=None if args.compact else 4,
        resolve_string_table=not args.raw_keys,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
