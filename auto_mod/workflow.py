"""Auto Mod orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from filefinder.core.neox_xml import neox_bytes_to_text
from filefinder.lookup.thy import ThyLookupTable

from .assets import AssetIndex
from .gim_editor import EditFeedback, parse_gim_xml, patch_gim_tree, write_gim_tree
from .manifest import write_mod_manifest


@dataclass(frozen=True)
class AutoModResult:
    output_path: Path
    manifest_path: Path
    feedback: EditFeedback


class AutoModPrompts:
    def ask_dependency_path(self, tag_name: str, raw_value: str, extension: str) -> str | None:
        raise NotImplementedError

    def ask_socket_path(self, socket_name: str, predicted_path: str) -> str | None:
        raise NotImplementedError

    def ask_mod_name(self) -> str | None:
        raise NotImplementedError

    def ask_mod_key(self, archive_stem: str, default_key: str) -> str | None:
        raise NotImplementedError

    def ask_mod_key_conflict(self, conflicting_key: str, existing_value: str, new_value: str) -> str | None:
        raise NotImplementedError


def run_auto_mod(
    *,
    game_root: Path,
    output_folder: Path,
    gim_path: str,
    prompts: AutoModPrompts,
) -> AutoModResult:
    output_folder = output_folder.resolve()
    asset_index = AssetIndex(game_root)
    extracted = asset_index.extract(gim_path)
    parsed = extracted.request
    if not parsed.normalized_path.lower().endswith(".gim"):
        raise ValueError("Input asset path must point to a .gim file")

    xml_text = neox_bytes_to_text(extracted.data)
    tree = parse_gim_xml(xml_text)
    feedback = patch_gim_tree(
        tree,
        parsed_gim=parsed,
        game_root=game_root,
        output_folder=output_folder,
        asset_index=asset_index,
        prompt_dependency_path=_CachingDependencyPrompt(prompts).ask,
        prompt_socket_path=prompts.ask_socket_path,
    )
    output_path = output_folder / Path(parsed.normalized_path).name
    write_gim_tree(tree, output_path)
    mod_name = prompts.ask_mod_name()
    if not mod_name:
        raise RuntimeError("Mod name is required to create mod.json")
    manifest_path = write_mod_manifest(
        game_root=game_root,
        output_folder=output_folder,
        modded_gim_path=output_path,
        parsed_gim=parsed,
        mod_name=mod_name,
        prompt_mod_key=prompts.ask_mod_key,
        prompt_key_conflict=prompts.ask_mod_key_conflict,
    )
    return AutoModResult(output_path=output_path, manifest_path=manifest_path, feedback=feedback)


class _CachingDependencyPrompt:
    def __init__(self, prompts: AutoModPrompts) -> None:
        self.prompts = prompts
        self.cached_path: str | None = None

    def ask(self, tag_name: str, raw_value: str, extension: str) -> str | None:
        if self.cached_path is None:
            self.cached_path = self.prompts.ask_dependency_path(tag_name, raw_value, extension)
        if not self.cached_path:
            return None
        normalized = ThyLookupTable.normalize_path(self.cached_path)
        stem = normalized.rsplit(".", 1)[0]
        return f"{stem}.{extension}"
