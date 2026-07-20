"""High-level lookup and extraction workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from filefinder.archive.idx_wpk import LoadedEntry, extract_matching_entries
from filefinder.lookup.thy import LookupResult, ThyLookupTable

from .neox_xml import NEOX_BINARY_MAGIC, neox_bytes_to_text
from .paths import ParsedInput, discover_archives, output_path_for, parse_asset_path, resolve_thy_path


NX_XML_EXTENSIONS = {".gim", ".mtg", ".mtl"}


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


def _request_key(request: ParsedInput) -> tuple[str, str]:
    return (request.archive.prefix, request.normalized_path)


def _canonical_raw(request: ParsedInput) -> str:
    return f"{request.archive.prefix}/{request.normalized_path}"


def _with_extension(request: ParsedInput, extension: str) -> ParsedInput:
    stem = request.normalized_path.rsplit(".", 1)[0]
    return ParsedInput(
        raw_path=request.raw_path,
        archive=request.archive,
        normalized_path=ThyLookupTable.normalize_path(f"{stem}.{extension}"),
    )


def _is_tga_request(request: ParsedInput) -> bool:
    return request.normalized_path.lower().endswith(".tga")


def _lookup_requests(
    game_root: Path,
    requests: list[ParsedInput],
    *,
    strict_lookup: bool,
    optional_lookup_keys: set[tuple[str, str]] | None = None,
) -> list[AssetLookup]:
    optional_lookup_keys = optional_lookup_keys or set()
    grouped: dict[str, list[ParsedInput]] = {}
    for request in requests:
        grouped.setdefault(request.archive.prefix, []).append(request)

    lookups: list[AssetLookup] = []
    for grouped_requests in grouped.values():
        try:
            lookups.extend(_lookup_group(game_root, grouped_requests))
            continue
        except Exception:
            pass
        for request in grouped_requests:
            try:
                lookups.extend(_lookup_group(game_root, [request]))
            except Exception:
                if strict_lookup and _request_key(request) not in optional_lookup_keys:
                    raise
                continue
    return lookups


def _extract_lookups(
    lookups: list[AssetLookup],
    *,
    output_root: Path,
    auto_decode_nx_xml: bool,
) -> tuple[list[WrittenAsset], list[MissingAsset]]:
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
            found = extract_matching_entries(idx_path, remaining)
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
            output_data = _maybe_decode_nx_xml(
                entry.data,
                item.request.normalized_path,
                auto_decode=auto_decode_nx_xml,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(output_data)
            written.append(
                WrittenAsset(
                    request=item.request,
                    hash128_hex=hash_hex,
                    source_archive=source_archive,
                    output_path=output_path,
                    byte_count=len(output_data),
                )
            )

    return written, missing


def _maybe_decode_nx_xml(data: bytes, normalized_path: str, *, auto_decode: bool) -> bytes:
    if not auto_decode:
        return data
    if Path(normalized_path).suffix.lower() not in NX_XML_EXTENSIONS:
        return data
    if not data.startswith(NEOX_BINARY_MAGIC):
        return data
    return neox_bytes_to_text(data).encode("utf-8")


def extract_assets(
    game_root: Path,
    raw_paths: list[str],
    *,
    output_root: Path,
    strict_lookup: bool = True,
    auto_decode_nx_xml: bool = True,
) -> ExtractionReport:
    archives = discover_archives(game_root)
    if not archives:
        raise FileNotFoundError(
            f"No common .idx archives were found in {game_root / 'res'} "
            f"and {game_root / 'Documents' / 'res'}"
        )

    parsed = [parse_asset_path(raw_path, archives) for raw_path in raw_paths]
    primary_requests: list[ParsedInput] = []
    tga_fallbacks: dict[tuple[str, str], ParsedInput] = {}
    for request in parsed:
        if _is_tga_request(request):
            primary = _with_extension(request, "dds")
            primary_requests.append(primary)
            tga_fallbacks[_request_key(primary)] = request
            continue
        primary_requests.append(request)

    lookups = _lookup_requests(
        game_root,
        primary_requests,
        strict_lookup=strict_lookup,
        optional_lookup_keys=set(tga_fallbacks),
    )
    primary_lookup_keys = {_request_key(item.request) for item in lookups}
    written, missing = _extract_lookups(
        lookups,
        output_root=output_root,
        auto_decode_nx_xml=auto_decode_nx_xml,
    )

    written_keys = {_request_key(item.request) for item in written}
    fallback_requests = [
        fallback
        for primary_key, fallback in tga_fallbacks.items()
        if primary_key not in written_keys
    ]
    fallback_lookups = _lookup_requests(game_root, fallback_requests, strict_lookup=False)
    fallback_lookup_raws = {item.request.raw_path for item in fallback_lookups}
    if strict_lookup:
        unresolved_lookup_requests = [
            request
            for request in fallback_requests
            if _request_key(_with_extension(request, "dds")) not in primary_lookup_keys
            and request.raw_path not in fallback_lookup_raws
        ]
        if unresolved_lookup_requests:
            _lookup_requests(game_root, unresolved_lookup_requests, strict_lookup=True)
    fallback_written, fallback_missing = _extract_lookups(
        fallback_lookups,
        output_root=output_root,
        auto_decode_nx_xml=auto_decode_nx_xml,
    )

    missing = [
        item
        for item in missing
        if item.request.raw_path not in fallback_lookup_raws
    ]
    lookups.extend(fallback_lookups)
    written.extend(fallback_written)
    missing.extend(fallback_missing)

    return ExtractionReport(
        lookups=tuple(lookups),
        written=tuple(written),
        missing=tuple(missing),
    )
