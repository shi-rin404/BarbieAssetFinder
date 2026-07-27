from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


API_VERSION = 1
ASSET_PREFIX = "idvmi-api"
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
        description="Build the IDVMI-Tools built-in API release bundle."
    )
    parser.add_argument("--version", required=True, help="IDVMI API version, for example 0.2.0")
    parser.add_argument(
        "--min-addon-version",
        default="8.1.0",
        help="Minimum IDVMI-Tools add-on version required by this IDVMI API bundle.",
    )
    parser.add_argument(
        "--output-dir",
        default="dist",
        help="Directory where the zip file will be written.",
    )
    parser.add_argument("--notes", default="", help="Short release notes for the manifest.")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def package_files(source_root: Path) -> list[str]:
    return [
        path.relative_to(source_root.parent).as_posix()
        for path in sorted(source_root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    ]


def payload_sha256(source_root: Path, files: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((source_root.parent / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_zip(source_root: Path, archive_path: Path, manifest_name: str, manifest: dict) -> None:
    files = package_files(source_root)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(source_root.parent / relative, relative)
        archive.writestr(manifest_name, json.dumps(manifest, indent=4) + "\n")


def source_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root(),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def main() -> int:
    args = parse_args()
    output_dir = (repo_root() / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    version = args.version.strip().lstrip("v")
    archive_name = f"{ASSET_PREFIX}-v{version}.zip"
    manifest_name = f"{ASSET_PREFIX}-v{version}.json"
    archive_path = output_dir / archive_name

    with tempfile.TemporaryDirectory(prefix="idvmi_asset_finder_bundle_") as temp_dir:
        package_root = build_staging_tree(Path(temp_dir), version)
        files = package_files(package_root)
        manifest = {
            "version": version,
            "api_version": API_VERSION,
            "min_addon_version": args.min_addon_version.strip().lstrip("v"),
            "sha256": payload_sha256(package_root, files),
            "archive_name": archive_name,
            "source_commit": source_commit(),
            "notes": args.notes,
        }
        write_zip(package_root, archive_path, manifest_name, manifest)

    print(f"Wrote {archive_path}")
    print(f"Embedded {manifest_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
