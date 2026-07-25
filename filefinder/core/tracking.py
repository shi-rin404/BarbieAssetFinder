"""Linked asset discovery for GUI file tracking."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
import posixpath
import re
import sys
from typing import Callable
import xml.etree.ElementTree as ET

from filefinder.archive.idx_wpk import ArchiveIndexCache
from filefinder.lookup.thy import ThyLookupTable

from .extract import ExtractionReport, ParsedInput, extract_assets
from .neox_xml import neox_bytes_to_text
from .paths import discover_archives, output_path_for, parse_asset_path


CONVERTIBLE_TYPES = {
    "Mesh": "mesh",
    "GIM": "gim",
    "MTG": "mtg",
    "STB": "stb",
}
TEXTURE_KEYS = {
    "Diffuse": "Tex0",
    "Metal": "TexMetal",
    "Normal": "TexNormal",
}
TEXTURE_EXTENSIONS = {".dds", ".tga", ".png", ".jpg", ".jpeg", ".bmp"}
TrackingErrorCallback = Callable[[str, Exception], None]
_TRACKING_ERROR_CALLBACK: ContextVar[TrackingErrorCallback | None] = ContextVar(
    "tracking_error_callback",
    default=None,
)


def _log_suppressed_error(context: str, exc: Exception) -> None:
    print(f"[File Tracker] {context}: {type(exc).__name__}: {exc}", file=sys.stderr)
    callback = _TRACKING_ERROR_CALLBACK.get()
    if callback is not None:
        callback(context, exc)


def extract_assets_with_tracking(
    game_root: Path,
    raw_paths: list[str],
    *,
    output_root: Path,
    file_types: set[str],
    texture_types: set[str],
    auto_decode_nx_xml: bool = True,
    suppressed_error_callback: TrackingErrorCallback | None = None,
    index_cache: ArchiveIndexCache | None = None,
) -> ExtractionReport:
    token = _TRACKING_ERROR_CALLBACK.set(suppressed_error_callback)
    try:
        return _extract_assets_with_tracking_impl(
            game_root,
            raw_paths,
            output_root=output_root,
            file_types=file_types,
            texture_types=texture_types,
            auto_decode_nx_xml=auto_decode_nx_xml,
            index_cache=index_cache,
        )
    finally:
        _TRACKING_ERROR_CALLBACK.reset(token)


def _extract_assets_with_tracking_impl(
    game_root: Path,
    raw_paths: list[str],
    *,
    output_root: Path,
    file_types: set[str],
    texture_types: set[str],
    auto_decode_nx_xml: bool,
    index_cache: ArchiveIndexCache | None,
) -> ExtractionReport:
    archives = discover_archives(game_root)
    if not archives:
        raise FileNotFoundError(
            f"No common .idx archives were found in {game_root / 'res'} "
            f"and {game_root / 'Documents' / 'res'}"
        )

    source_requests = [parse_asset_path(raw_path, archives) for raw_path in raw_paths]
    source_raws = {_canonical_raw(request) for request in source_requests}
    wanted_raws = _wanted_raws(source_raws)
    reports = [
        extract_assets(
            game_root,
            raw_paths,
            output_root=output_root,
            auto_decode_nx_xml=auto_decode_nx_xml,
            index_cache=index_cache,
        )
    ]

    direct_raws = _direct_conversion_raws(source_requests, file_types)
    wanted_raws.update(_wanted_raws(direct_raws))

    needs_mtg_trace = bool({"MTL", "Texture"} & file_types)
    mtg_trace_raws = _mtg_trace_raws(source_requests) if needs_mtg_trace else set()
    mtg_intermediate_raws = mtg_trace_raws - wanted_raws
    first_pass_raws = sorted((direct_raws | mtg_trace_raws) - source_raws)
    if first_pass_raws:
        reports.append(
            _extract_optional_assets(
                game_root,
                first_pass_raws,
                output_root=output_root,
                auto_decode_nx_xml=auto_decode_nx_xml,
                index_cache=index_cache,
            )
        )

    mtg_requests = _parse_existing(first_pass_raws, archives)
    mtg_requests.extend(request for request in source_requests if _extension(request.normalized_path) == "mtg")
    mtl_raws = _linked_mtl_raws(mtg_requests, output_root, archives)
    if "MTL" in file_types:
        wanted_raws.update(_wanted_raws(mtl_raws))

    needs_mtl_trace = "Texture" in file_types
    mtl_trace_raws = set(mtl_raws) if needs_mtl_trace else set()
    mtl_trace_raws.update(
        _canonical_raw(request) for request in source_requests if _extension(request.normalized_path) == "mtl"
    )
    mtl_intermediate_raws = mtl_trace_raws - wanted_raws
    second_pass_raws = sorted((mtl_raws | mtl_trace_raws) - _written_raws(reports))
    if second_pass_raws:
        reports.append(
            _extract_optional_assets(
                game_root,
                second_pass_raws,
                output_root=output_root,
                auto_decode_nx_xml=auto_decode_nx_xml,
                index_cache=index_cache,
            )
        )

    if needs_mtl_trace:
        mtl_requests = _parse_existing(second_pass_raws, archives)
        mtl_requests.extend(request for request in source_requests if _extension(request.normalized_path) == "mtl")
        texture_raws = _linked_texture_raws(
            mtl_requests,
            output_root,
            archives,
            texture_types,
        )
        wanted_raws.update(_wanted_raws(texture_raws))
        if texture_raws:
            reports.append(
                _extract_optional_assets(
                    game_root,
                    sorted(texture_raws),
                    output_root=output_root,
                    auto_decode_nx_xml=auto_decode_nx_xml,
                    index_cache=index_cache,
                )
            )

    report = _merge_reports(reports)
    intermediates = (mtg_intermediate_raws | mtl_intermediate_raws) - wanted_raws
    _delete_intermediate_outputs(report, output_root, intermediates)
    return _filter_visible_report(report, wanted_raws, intermediates)


def _extract_optional_assets(
    game_root: Path,
    raw_paths: list[str],
    *,
    output_root: Path,
    auto_decode_nx_xml: bool,
    index_cache: ArchiveIndexCache | None,
) -> ExtractionReport:
    return extract_assets(
        game_root,
        _dedupe(raw_paths),
        output_root=output_root,
        strict_lookup=False,
        auto_decode_nx_xml=auto_decode_nx_xml,
        index_cache=index_cache,
    )


def _direct_conversion_raws(requests: list[ParsedInput], file_types: set[str]) -> set[str]:
    target_extensions = {
        extension for label, extension in CONVERTIBLE_TYPES.items() if label in file_types
    }
    if not target_extensions:
        return set()

    raws: set[str] = set()
    for request in requests:
        if _extension(request.normalized_path) not in set(CONVERTIBLE_TYPES.values()):
            continue
        for extension in target_extensions:
            raws.add(_raw_with_extension(request, extension))
    return raws


def _mtg_trace_raws(requests: list[ParsedInput]) -> set[str]:
    raws: set[str] = set()
    for request in requests:
        extension = _extension(request.normalized_path)
        if extension == "mtg":
            raws.add(_canonical_raw(request))
        elif extension in set(CONVERTIBLE_TYPES.values()):
            raws.add(_raw_with_extension(request, "mtg"))
    return raws


def _linked_mtl_raws(
    mtg_requests: list[ParsedInput],
    output_root: Path,
    archives: dict[str, object],
) -> set[str]:
    raws: set[str] = set()
    for request in mtg_requests:
        output_path = output_path_for(output_root, request.archive.prefix, request.normalized_path)
        if not output_path.is_file():
            continue
        for reference in _extract_mtl_references(output_path):
            raw = _reference_to_raw(reference, request, archives)
            if raw is not None:
                raws.add(raw)
    return raws


def _linked_texture_raws(
    mtl_requests: list[ParsedInput],
    output_root: Path,
    archives: dict[str, object],
    texture_types: set[str],
) -> set[str]:
    raws: set[str] = set()
    for request in mtl_requests:
        output_path = output_path_for(output_root, request.archive.prefix, request.normalized_path)
        if not output_path.is_file():
            continue
        for reference in _extract_texture_references(output_path, texture_types):
            raw = _reference_to_raw(reference, request, archives)
            if raw is None:
                continue
            raws.add(raw)
    return raws


def _extract_mtl_references(path: Path) -> set[str]:
    text = neox_bytes_to_text(path.read_bytes())
    references = {
        value
        for value in _xml_attribute_values(text, attribute_names={"Path"})
        if value.lower().endswith(".mtl")
    }
    if references:
        return references
    return set(re.findall(r"""["']([^"']+\.mtl)["']""", text, flags=re.IGNORECASE))


def _extract_texture_references(path: Path, texture_types: set[str]) -> set[str]:
    text = neox_bytes_to_text(path.read_bytes())
    roots = _parse_xml_roots(text)
    grab_all = "Grab All" in texture_types
    target_tags = set(TEXTURE_KEYS.values())
    if not grab_all:
        target_tags = {tag for label, tag in TEXTURE_KEYS.items() if label in texture_types}

    references: set[str] = set()
    for root in roots:
        for element in root.iter():
            if grab_all:
                if not element.tag.lower().startswith("tex"):
                    continue
            elif element.tag not in target_tags:
                continue
            value = element.attrib.get("Value", "").strip()
            if _looks_like_texture(value):
                references.add(value)

    if references:
        return references

    if grab_all:
        pattern = r"""<Tex[^>\s]*\b[^>]*\bValue=["']([^"']+)["']"""
    else:
        tag_pattern = "|".join(re.escape(tag) for tag in target_tags)
        if not tag_pattern:
            return set()
        pattern = rf"""<({tag_pattern})\b[^>]*\bValue=["']([^"']+)["']"""

    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    values = [match if isinstance(match, str) else match[-1] for match in matches]
    return {value for value in values if _looks_like_texture(value)}


def _xml_attribute_values(text: str, *, attribute_names: set[str]) -> set[str]:
    values: set[str] = set()
    for root in _parse_xml_roots(text):
        for element in root.iter():
            for attribute_name in attribute_names:
                value = element.attrib.get(attribute_name)
                if value:
                    values.add(value.strip())
    return values


def _parse_xml_roots(text: str) -> list[ET.Element]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return [ET.fromstring(stripped)]
    except ET.ParseError as exc:
        wrapped = f"<FileFinderRoot>{stripped}</FileFinderRoot>"
        try:
            return [ET.fromstring(wrapped)]
        except ET.ParseError as wrapped_exc:
            _log_suppressed_error(f"XML parse failed, including wrapped fallback after {exc}", wrapped_exc)
            return []


def _reference_to_raw(
    reference: str,
    base_request: ParsedInput,
    archives: dict[str, object],
) -> str | None:
    normalized = ThyLookupTable.normalize_path(reference)
    if not normalized:
        return None
    try:
        parse_asset_path(normalized, archives)
        return normalized
    except Exception as exc:
        _log_suppressed_error(f"Reference is not a full archive path: {reference!r}", exc)

    parent = posixpath.dirname(base_request.normalized_path)
    joined = posixpath.normpath(posixpath.join(parent, normalized)).replace("\\", "/")
    raw = f"{base_request.archive.prefix}/{joined}"
    try:
        parse_asset_path(raw, archives)
    except Exception as exc:
        _log_suppressed_error(f"Reference could not be resolved relative to {base_request.raw_path!r}: {reference!r}", exc)
        return None
    return raw


def _raw_with_extension(request: ParsedInput, extension: str) -> str:
    path = request.normalized_path.rsplit(".", 1)[0]
    return f"{request.archive.prefix}/{path}.{extension}"


def _canonical_raw(request: ParsedInput) -> str:
    return f"{request.archive.prefix}/{request.normalized_path}"


def _wanted_raws(raws: set[str]) -> set[str]:
    wanted: set[str] = set()
    for raw in raws:
        wanted.add(raw)
        if raw.lower().endswith(".tga"):
            wanted.add(f"{raw[:-4]}.dds")
    return wanted


def _extension(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def _looks_like_texture(value: str) -> bool:
    if not value:
        return False
    return Path(value.replace("\\", "/")).suffix.lower() in TEXTURE_EXTENSIONS


def _parse_existing(raw_paths: set[str] | list[str], archives: dict[str, object]) -> list[ParsedInput]:
    parsed: list[ParsedInput] = []
    for raw_path in _dedupe(raw_paths):
        try:
            parsed.append(parse_asset_path(raw_path, archives))
        except Exception as exc:
            _log_suppressed_error(f"Could not parse tracked raw path: {raw_path!r}", exc)
            continue
    return parsed


def _written_raws(reports: list[ExtractionReport]) -> set[str]:
    return {_canonical_raw(item.request) for report in reports for item in report.written}


def _merge_reports(reports: list[ExtractionReport]) -> ExtractionReport:
    lookups: list[AssetLookup] = []
    written: list[WrittenAsset] = []
    missing: list[MissingAsset] = []
    seen_written: set[tuple[str, Path]] = set()

    for report in reports:
        lookups.extend(report.lookups)
        for item in report.written:
            key = (_canonical_raw(item.request), item.output_path)
            if key in seen_written:
                continue
            seen_written.add(key)
            written.append(item)
        missing.extend(report.missing)

    return ExtractionReport(lookups=tuple(lookups), written=tuple(written), missing=tuple(missing))


def _filter_visible_report(
    report: ExtractionReport,
    wanted_raws: set[str],
    intermediate_raws: set[str],
) -> ExtractionReport:
    return ExtractionReport(
        lookups=tuple(
            item
            for item in report.lookups
            if _canonical_raw(item.request) in wanted_raws
            or _canonical_raw(item.request) not in intermediate_raws
        ),
        written=tuple(
            item
            for item in report.written
            if _canonical_raw(item.request) in wanted_raws
            and item.output_path.exists()
        ),
        missing=tuple(
            item
            for item in report.missing
            if _canonical_raw(item.request) in wanted_raws
        ),
    )


def _delete_intermediate_outputs(
    report: ExtractionReport,
    output_root: Path,
    intermediate_raws: set[str],
) -> None:
    output_root_resolved = output_root.resolve(strict=False)
    for item in report.written:
        if _canonical_raw(item.request) not in intermediate_raws:
            continue
        try:
            item.output_path.resolve(strict=False).relative_to(output_root_resolved)
        except ValueError:
            continue
        if item.output_path.is_file():
            item.output_path.unlink()


def _dedupe(values) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
