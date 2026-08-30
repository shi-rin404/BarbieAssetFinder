from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from filefinder.version import __version__ as DEFAULT_VERSION


API_VERSION = 1
REQUIRED_FILES = (
    "archive/__init__.py",
    "archive/codecs.py",
    "archive/idx_wpk.py",
    "core/__init__.py",
    "core/paths.py",
    "lookup/__init__.py",
    "lookup/flatbuffers.py",
    "lookup/thy.py",
    "lookup/xxhash.py",
)


ASSETS_PY = '''from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .filefinder.archive.idx_wpk import ArchiveIndexCache, extract_matching_entries
from .filefinder.core.paths import (
    ParsedInput,
    discover_archives,
    parse_asset_path,
    resolve_thy_path,
)
from .filefinder.lookup.thy import ThyLookupTable


@dataclass(frozen=True)
class ExtractedAsset:
    request: ParsedInput
    data: bytes
    source_archive: Path


class AssetIndex:
    def __init__(self, game_root: Path) -> None:
        self.game_root = game_root
        self.archives = discover_archives(game_root)
        if not self.archives:
            raise FileNotFoundError(
                f"No common .idx archives were found in {game_root / 'res'} "
                f"and {game_root / 'Documents' / 'res'}"
            )
        self._thy_cache: dict[str, ThyLookupTable] = {}
        self._index_cache = ArchiveIndexCache()

    def parse(self, raw_path: str) -> ParsedInput:
        return parse_asset_path(raw_path, self.archives)

    def extract(self, raw_path: str) -> ExtractedAsset:
        if self._is_tga_path(raw_path):
            dds_path = self._replace_extension(raw_path, ".dds")
            try:
                return self._extract_one(dds_path)
            except Exception as dds_error:
                try:
                    return self._extract_one(raw_path)
                except Exception as tga_error:
                    raise FileNotFoundError(
                        f"Asset was not found as DDS or TGA: "
                        f"{dds_path} ({dds_error}); {raw_path} ({tga_error})"
                    ) from tga_error
        return self._extract_one(raw_path)

    def exists(self, raw_path: str) -> bool:
        try:
            self.extract(raw_path)
        except Exception:
            return False
        return True

    def _extract_one(self, raw_path: str) -> ExtractedAsset:
        request = self.parse(raw_path)
        lookup = self._lookup(request)
        remaining = {lookup.hash128_hex}
        for idx_path in request.archive.idx_paths:
            found = extract_matching_entries(
                idx_path,
                remaining,
                index_cache=self._index_cache,
            )
            match = found.get(lookup.hash128_hex)
            if match is not None:
                return ExtractedAsset(
                    request=request,
                    data=match.data,
                    source_archive=idx_path,
                )
        raise FileNotFoundError(
            f"Asset was resolved in THY but not found in IDX/WPK: {raw_path}"
        )

    def _lookup(self, request: ParsedInput):
        table = self._thy_cache.get(request.archive.stem)
        if table is None:
            table = ThyLookupTable(resolve_thy_path(self.game_root, request.archive.stem))
            self._thy_cache[request.archive.stem] = table
        return table.lookup(request.normalized_path)

    @staticmethod
    def _is_tga_path(raw_path: str) -> bool:
        return raw_path.strip().replace("\\\\", "/").lower().endswith(".tga")

    @staticmethod
    def _replace_extension(raw_path: str, extension: str) -> str:
        normalized = raw_path.strip()
        stem = normalized.rsplit(".", 1)[0]
        return f"{stem}{extension}"
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the current FileFinder asset API into an IDVMI-Tools add-on tree."
    )
    parser.add_argument(
        "addon_root",
        type=Path,
        help="Path to the IDVMI-Tools add-on root.",
    )
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help=f"API version written into asset_lookup.__version__. Default: {DEFAULT_VERSION}",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return REPO_ROOT


def copy_required_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def build_staging_tree(staging_root: Path, version: str) -> Path:
    root = repo_root()
    package_root = staging_root / "asset_lookup"
    filefinder_root = package_root / "filefinder"
    package_root.mkdir(parents=True, exist_ok=True)
    filefinder_root.mkdir(parents=True, exist_ok=True)

    (package_root / "__init__.py").write_text(
        '"""Built-in asset lookup and extraction helpers for remote NeoX imports."""\n\n'
        f'__version__ = "{version}"\n'
        f"__api_version__ = {API_VERSION}\n",
        encoding="utf-8",
    )
    (package_root / "assets.py").write_text(ASSETS_PY, encoding="utf-8")
    (filefinder_root / "__init__.py").write_text('"""Portable FileFinder core for IDVMI-Tools."""\n', encoding="utf-8")

    for relative in REQUIRED_FILES:
        copy_required_file(root / "filefinder" / relative, filefinder_root / relative)
    return package_root


def validate_addon_root(addon_root: Path) -> Path:
    root = addon_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"IDVMI-Tools add-on root was not found: {root}")
    if not (root / "__init__.py").is_file():
        raise FileNotFoundError(f"Add-on root does not contain __init__.py: {root}")
    if not (root / "neox_tools").is_dir():
        raise FileNotFoundError(f"Add-on root does not contain neox_tools: {root}")
    return root


def count_files(root: Path) -> int:
    return sum(
        1
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() not in {".pyc", ".pyo"}
    )


def install_asset_lookup(package_root: Path, addon_root: Path) -> tuple[Path, int]:
    destination = (addon_root / "neox_tools" / "asset_lookup").resolve()
    try:
        destination.relative_to(addon_root)
    except ValueError as exc:
        raise ValueError(f"Refusing to install outside add-on root: {destination}") from exc

    if destination.exists():
        shutil.rmtree(destination)

    shutil.copytree(
        package_root,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    return destination, count_files(destination)


def main() -> int:
    args = parse_args()
    addon_root = validate_addon_root(args.addon_root)
    version = args.version.strip().lstrip("v")

    with tempfile.TemporaryDirectory(prefix="idvmi_asset_lookup_install_") as temp_dir:
        package_root = build_staging_tree(Path(temp_dir), version)
        destination, installed_files = install_asset_lookup(package_root, addon_root)

    print(f"Installed asset_lookup v{version} to {destination}")
    print(f"Installed files: {installed_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
