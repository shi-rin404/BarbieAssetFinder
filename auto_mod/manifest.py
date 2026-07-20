"""Mod loader manifest generation."""

from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
import posixpath

from filefinder.core.paths import ParsedInput

from .paths import is_boss_prefab_name


def write_mod_manifest(
    *,
    game_root: Path,
    output_folder: Path,
    modded_gim_path: Path,
    parsed_gim: ParsedInput,
    mod_name: str,
    prompt_mod_key,
    prompt_key_conflict=None,
) -> Path:
    manifest_path = output_folder / "mod.json"
    manifest = load_existing_manifest(manifest_path)
    manifest["name"] = mod_name
    manifest.move_to_end("name", last=False)

    relative_gim = documents_res_relative_path(game_root, modded_gim_path)
    entry_key = manifest_entry_key(parsed_gim, prompt_mod_key)
    if entry_key is not None:
        entry_key = resolve_manifest_key(manifest, entry_key, relative_gim, prompt_key_conflict)
        manifest[entry_key] = relative_gim

    manifest_path.write_text(
        json.dumps(manifest, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def resolve_manifest_key(
    manifest: OrderedDict,
    entry_key: str,
    relative_gim: str,
    prompt_key_conflict,
) -> str:
    selected_key = entry_key
    while selected_key in manifest and manifest[selected_key] != relative_gim:
        if prompt_key_conflict is None:
            return selected_key
        replacement = prompt_key_conflict(selected_key, manifest[selected_key], relative_gim)
        if not replacement:
            raise RuntimeError("Custom mod key is required to resolve mod.json key conflict")
        selected_key = replacement.strip()
    return selected_key


def load_existing_manifest(manifest_path: Path) -> OrderedDict:
    if not manifest_path.is_file():
        return OrderedDict()
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=OrderedDict)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Existing mod.json is not valid JSON: {manifest_path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Existing mod.json must contain a JSON object: {manifest_path}")
    return OrderedDict(loaded)


def documents_res_relative_path(game_root: Path, path: Path) -> str:
    documents_res = (game_root / "Documents" / "res").resolve()
    return path.resolve().relative_to(documents_res).as_posix()


def manifest_entry_key(parsed_gim: ParsedInput, prompt_mod_key) -> str | None:
    filename = posixpath.basename(parsed_gim.normalized_path).lower()
    if parsed_gim.archive.stem == "chr_prop":
        return prompt_mod_key("chr_prop", "item")
    if is_boss_prefab_name(filename):
        return "skin"
    if parsed_gim.archive.stem == "chr_boss":
        return prompt_mod_key("chr_boss", "item")
    return None
