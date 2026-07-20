"""GIM XML patching rules for Auto Mod."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET

from filefinder.core.paths import ParsedInput
from filefinder.lookup.thy import ThyLookupTable

from .assets import AssetIndex
from .hashing import object_id_for_name
from .paths import (
    asset_parent,
    documents_relative_path,
    resolve_dependency_asset_path,
    skin_name_from_gim,
    with_extension,
)


@dataclass
class EditFeedback:
    dependency_updates: list[str] = field(default_factory=list)
    socket_updates: list[str] = field(default_factory=list)
    skipped_sockets: list[str] = field(default_factory=list)


def parse_gim_xml(text: str) -> ET.ElementTree:
    return ET.ElementTree(ET.fromstring(text.strip()))


def patch_gim_tree(
    tree: ET.ElementTree,
    *,
    parsed_gim: ParsedInput,
    game_root: Path,
    output_folder: Path,
    asset_index: AssetIndex,
    prompt_dependency_path,
    prompt_socket_path,
) -> EditFeedback:
    root = tree.getroot()
    feedback = EditFeedback()
    mesh_path = with_extension(parsed_gim, "mesh")
    mtg_path = with_extension(parsed_gim, "mtg")
    root.set("Mesh", mesh_path)
    mtg_tag = _ensure_child(root, "MtgFile")
    mtg_tag.set("MtgPath", mtg_path)

    _rewrite_file_dependency(
        root,
        tag_name="SkeletonFile",
        extension="skeleton",
        parsed_gim=parsed_gim,
        game_root=game_root,
        output_folder=output_folder,
        feedback=feedback,
        prompt_dependency_path=prompt_dependency_path,
    )
    _rewrite_file_dependency(
        root,
        tag_name="AnimationConfigFile",
        extension="animconfig",
        parsed_gim=parsed_gim,
        game_root=game_root,
        output_folder=output_folder,
        feedback=feedback,
        prompt_dependency_path=prompt_dependency_path,
    )
    _bind_socket_objects(
        root,
        parsed_gim=parsed_gim,
        asset_index=asset_index,
        prompt_socket_path=prompt_socket_path,
        feedback=feedback,
    )
    return feedback


def write_gim_tree(tree: ET.ElementTree, output_path: Path) -> None:
    ET.indent(tree, space="    ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=False)


def _rewrite_file_dependency(
    root: ET.Element,
    *,
    tag_name: str,
    extension: str,
    parsed_gim: ParsedInput,
    game_root: Path,
    output_folder: Path,
    feedback: EditFeedback,
    prompt_dependency_path,
) -> None:
    parent = _ensure_child(root, tag_name)
    file_name = _ensure_child(parent, "FileName")
    raw_value = file_name.attrib.get("Value") or file_name.attrib.get("value") or ""
    asset_path = resolve_dependency_asset_path(
        parsed_gim=parsed_gim,
        raw_value=raw_value,
        extension=extension,
    )
    if asset_path is None:
        asset_path = prompt_dependency_path(tag_name, raw_value, extension)
    if not asset_path:
        return

    relative_value = documents_relative_path(game_root, output_folder, asset_path)
    file_name.attrib.pop("value", None)
    file_name.set("Value", relative_value)
    feedback.dependency_updates.append(f"{tag_name}: {relative_value}")


def _bind_socket_objects(
    root: ET.Element,
    *,
    parsed_gim: ParsedInput,
    asset_index: AssetIndex,
    prompt_socket_path,
    feedback: EditFeedback,
) -> None:
    gim_name = posixpath.splitext(posixpath.basename(parsed_gim.normalized_path))[0]
    gim_folder = f"{parsed_gim.archive.prefix}/{asset_parent(parsed_gim.normalized_path)}"
    sockets = [element for element in root.iter() if re.fullmatch(r"Socket_\d+", element.tag)]
    existing_ids = {
        value.lower()
        for element in root.iter("Object")
        for value in [element.attrib.get("Id", "")]
        if value
    }

    updates = 0
    for socket in sockets:
        socket_name = socket.attrib.get("Name", "")
        direct_match = re.fullmatch(rf"{re.escape(gim_name)}_([a-z]+)", socket_name)
        if not direct_match:
            continue
        object_name = direct_match.group(1)
        target_path = f"{gim_folder}/{gim_name}_{object_name}.gim"
        resolved = _resolve_socket_target(socket_name, target_path, asset_index, prompt_socket_path)
        if not resolved:
            feedback.skipped_sockets.append(socket_name)
            continue
        _upsert_socket_object(socket, socket_name, resolved, existing_ids)
        feedback.socket_updates.append(f"{socket_name}: {resolved}")
        updates += 1

    if updates:
        return

    skin_name = skin_name_from_gim(posixpath.basename(parsed_gim.normalized_path))
    if not skin_name:
        return
    for socket in sockets:
        socket_name = socket.attrib.get("Name", "")
        match = re.fullmatch(rf"(?:const_)?{re.escape(skin_name)}_([a-z]+)", socket_name)
        if not match:
            continue
        object_name = match.group(1)
        target_path = f"{gim_folder}/{gim_name}_{object_name}.gim"
        resolved = _resolve_socket_target(socket_name, target_path, asset_index, prompt_socket_path)
        if not resolved:
            feedback.skipped_sockets.append(socket_name)
            continue
        _upsert_socket_object(socket, socket_name, resolved, existing_ids)
        feedback.socket_updates.append(f"{socket_name}: {resolved}")


def _resolve_socket_target(
    socket_name: str,
    predicted_path: str,
    asset_index: AssetIndex,
    prompt_socket_path,
) -> str | None:
    predicted_path = ThyLookupTable.normalize_path(predicted_path)
    if asset_index.exists(predicted_path):
        return predicted_path
    user_path = prompt_socket_path(socket_name, predicted_path)
    if not user_path:
        return None
    normalized = ThyLookupTable.normalize_path(user_path)
    return normalized if asset_index.exists(normalized) else None


def _upsert_socket_object(
    socket: ET.Element,
    socket_name: str,
    uri: str,
    existing_ids: set[str],
) -> None:
    target = None
    for child in socket.findall("Object"):
        if child.attrib.get("Name") == socket_name or child.attrib.get("Uri", "").lower().endswith(".gim"):
            target = child
            break
    if target is None:
        target = ET.SubElement(socket, "Object")

    object_id = object_id_for_name(socket_name, existing_ids)
    existing_ids.add(object_id)
    target.set("CastShadow", "true")
    target.set("Id", object_id)
    target.set("Inherit", "263")
    target.set("Loading", "4")
    target.set("Name", socket_name)
    target.set("Uri", uri)


def _ensure_child(parent: ET.Element, tag_name: str) -> ET.Element:
    child = parent.find(tag_name)
    if child is None:
        child = ET.SubElement(parent, tag_name)
    return child
