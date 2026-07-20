"""High-level lookup and extraction workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from filefinder.archive.idx_wpk import LoadedEntry, extract_matching_entries
from filefinder.lookup.thy import LookupResult, ThyLookupTable

from .paths import ParsedInput, discover_archives, output_path_for, parse_asset_path, resolve_thy_path


@dataclass(frozen=True)
class AssetLookup:
    request: ParsedInput
    lookup: LookupResult


@dataclass(frozen=True)
class WrittenAsset:
    request: ParsedInput
    hash128_hex: str
    source_archive: Path
    output_path: Path
    byte_count: int


@dataclass(frozen=True)
class MissingAsset:
    request: ParsedInput
    hash128_hex: str


@dataclass(frozen=True)
class ExtractionReport:
    lookups: tuple[AssetLookup, ...]
    written: tuple[WrittenAsset, ...]
    missing: tuple[MissingAsset, ...]

    @property
    def ok(self) -> bool:
        return not self.missing


def _lookup_group(game_root: Path, requests: list[ParsedInput]) -> list[AssetLookup]:
    if not requests:
        return []

    archive = requests[0].archive
    thy_path = resolve_thy_path(game_root, archive.stem)
    table = ThyLookupTable(thy_path)
    return [
        AssetLookup(request=request, lookup=table.lookup(request.normalized_path))
        for request in requests
    ]


def extract_assets(
    game_root: Path,
    raw_paths: list[str],
    *,
    output_root: Path,
    decode: bool = True,
) -> ExtractionReport:
    archives = discover_archives(game_root)
    if not archives:
        raise FileNotFoundError(
            f"No common .idx archives were found in {game_root / 'res'} "
            f"and {game_root / 'Documents' / 'res'}"
        )

    parsed = [parse_asset_path(raw_path, archives) for raw_path in raw_paths]
    grouped: dict[str, list[ParsedInput]] = {}
    for request in parsed:
        grouped.setdefault(request.archive.prefix, []).append(request)

    lookups: list[AssetLookup] = []
    for requests in grouped.values():
        lookups.extend(_lookup_group(game_root, requests))

    written: list[WrittenAsset] = []
    missing: list[MissingAsset] = []
    lookups_by_prefix: dict[str, list[AssetLookup]] = {}
    for lookup in lookups:
        lookups_by_prefix.setdefault(lookup.request.archive.prefix, []).append(lookup)

    for prefix_lookups in lookups_by_prefix.values():
        archive = prefix_lookups[0].request.archive
        remaining = {item.lookup.hash128_hex for item in prefix_lookups}
        found_entries: dict[str, tuple[Path, LoadedEntry]] = {}

        for idx_path in archive.idx_paths:
            found = extract_matching_entries(idx_path, remaining, decode=decode)
            for hash_hex, entry in found.items():
                if hash_hex not in found_entries:
                    found_entries[hash_hex] = (idx_path, entry)
            remaining.difference_update(found.keys())
            if not remaining:
                break

        for item in prefix_lookups:
            hash_hex = item.lookup.hash128_hex
            match = found_entries.get(hash_hex)
            if match is None:
                missing.append(MissingAsset(request=item.request, hash128_hex=hash_hex))
                continue

            source_archive, entry = match
            output_path = output_path_for(
                output_root,
                item.request.archive.prefix,
                item.request.normalized_path,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(entry.data)
            written.append(
                WrittenAsset(
                    request=item.request,
                    hash128_hex=hash_hex,
                    source_archive=source_archive,
                    output_path=output_path,
                    byte_count=len(entry.data),
                )
            )

    return ExtractionReport(
        lookups=tuple(lookups),
        written=tuple(written),
        missing=tuple(missing),
    )

