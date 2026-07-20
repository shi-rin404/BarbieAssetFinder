"""Path derivation and relative-path rewrite helpers for Auto Mod."""

from __future__ import annotations

from pathlib import Path
import posixpath
import re

from filefinder.core.paths import ParsedInput
from filefinder.lookup.thy import ThyLookupTable


KNOWN_CHR_PLAYER_FILES = {
    "dm65_survivor_w": "chr/player/dm65_survivor_w/dm65_survivor_w.{extension}",
    "h55_survivor_m_zbs": "chr/player/dm65_survivor_m/h55_survivor_m_zbs/h55_survivor_m_zbs.{extension}",
    "dm65_survivor_girl": "chr/player/dm65_survivor_girl/dm65_survivor_girl.{extension}",
}


def with_extension(parsed: ParsedInput, extension: str) -> str:
    stem = parsed.normalized_path.rsplit(".", 1)[0]
    return f"{parsed.archive.prefix}/{stem}.{extension}"


def asset_parent(raw_asset_path: str) -> str:
    return posixpath.dirname(ThyLookupTable.normalize_path(raw_asset_path))


def documents_relative_path(game_root: Path, output_folder: Path, asset_path: str) -> str:
    documents_res = (game_root / "Documents" / "res").resolve()
    output_relative = output_folder.resolve().relative_to(documents_res).as_posix()
    return posixpath.relpath(ThyLookupTable.normalize_path(asset_path), output_relative).replace("\\", "/")


def resolve_dependency_asset_path(
    *,
    parsed_gim: ParsedInput,
    raw_value: str,
    extension: str,
) -> str | None:
    normalized_value = raw_value.replace("\\", "/").strip()
    if not normalized_value:
        return None

    basename = posixpath.basename(normalized_value)
    basename_stem = basename.rsplit(".", 1)[0] if "." in basename else basename

    if parsed_gim.archive.stem == "chr_player":
        lower_value = normalized_value.lower()
        for known_name, template in KNOWN_CHR_PLAYER_FILES.items():
            expected = f"{known_name}.{extension}"
            if lower_value.endswith(expected):
                return template.format(extension=extension)

    if parsed_gim.archive.stem == "chr_boss" and is_boss_prefab_name(posixpath.basename(parsed_gim.normalized_path)):
        root_folder = parsed_gim.normalized_path.split("/", 1)[0]
        file_name = basename_stem or root_folder
        return f"{parsed_gim.archive.prefix}/{root_folder}/{file_name}.{extension}"

    if parsed_gim.archive.stem == "chr_prop":
        root_folder = parsed_gim.normalized_path.split("/", 1)[0]
        if root_folder == basename_stem:
            return f"{parsed_gim.archive.prefix}/{root_folder}/{basename_stem}.{extension}"

    if "../" not in normalized_value:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(parsed_gim.normalized_path), normalized_value))
        return f"{parsed_gim.archive.prefix}/{joined}".replace("\\", "/")

    return None


def is_boss_prefab_name(filename: str) -> bool:
    return bool(re.fullmatch(r"[a-z]+_[cde]_[a-z]+\.gim", filename.lower()))


def skin_name_from_gim(filename: str) -> str | None:
    match = re.fullmatch(r"[a-z]+[cde]*_([a-z]+)\.gim", filename.lower())
    if match:
        return match.group(1)
    match = re.fullmatch(r"[a-z]+_[a-z]+_([a-z]+)\.gim", filename.lower())
    if match:
        return match.group(1)
    return None
