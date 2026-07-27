from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


UPDATER_API_VERSION = 1
ASSET_PREFIX = "filefinder"
INCLUDED_DIRS = ("filefinder", "auto_mod")
INCLUDED_FILES = (
    "cli.py",
    "gui.py",
    "auto_mod_gui.py",
    "requirements.txt",
    "README.md",
    "Z_GUI.bat",
    "Z_CLI.bat",
    "Z_Auto_Mod_GUI.bat",
)
EXCLUDED_PARTS = {
    ".git",
    ".agents",
    ".codex",
    "__pycache__",
    "user",
    "outputs",
    "tmp",
    "dist",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a FileFinderV2 self-update release bundle.")
    parser.add_argument("--version", required=True, help="Project version, for example 2.1.0")
    parser.add_argument(
        "--min-current-version",
        default="0.0.0",
        help="Minimum currently installed version that can apply this update.",
    )
    parser.add_argument("--output-dir", default="dist", help="Output directory for release files.")
    parser.add_argument("--notes", default="", help="Short release notes for the manifest.")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def should_include(path: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return path.is_file()


def copy_tree(source: Path, target: Path) -> list[str]:
    managed: list[str] = []
    root = repo_root()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(root)
        if not should_include(relative):
            continue
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)
        managed.append(relative.as_posix())
    return managed


def copy_file(source: Path, target: Path) -> str:
    relative = source.relative_to(repo_root())
    output = target / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)
    return relative.as_posix()


def build_staging_tree(staging_root: Path, version: str) -> tuple[Path, list[str]]:
    root = repo_root()
    managed: list[str] = []

    for directory in INCLUDED_DIRS:
        managed.extend(copy_tree(root / directory, staging_root))

    for filename in INCLUDED_FILES:
        source = root / filename
        if source.is_file():
            managed.append(copy_file(source, staging_root))

    version_path = staging_root / "filefinder" / "version.py"
    if version_path.is_file():
        version_path.write_text(
            '"""Central project version information."""\n\n'
            f'__version__ = "{version}"\n'
            f"UPDATER_API_VERSION = {UPDATER_API_VERSION}\n",
            encoding="utf-8",
        )

    managed = sorted(set(managed))
    if not managed:
        raise RuntimeError("No files were added to the release bundle")
    return staging_root, managed


def payload_sha256(source_root: Path, managed_files: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in managed_files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((source_root / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_zip(
    source_root: Path,
    managed_files: list[str],
    archive_path: Path,
    manifest_name: str,
    manifest: dict,
) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in managed_files:
            archive.write(source_root / relative, relative)
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
    version = args.version.strip().lstrip("v")
    output_dir = (repo_root() / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    archive_name = f"{ASSET_PREFIX}-v{version}.zip"
    manifest_name = f"{ASSET_PREFIX}-v{version}.json"
    archive_path = output_dir / archive_name

    with tempfile.TemporaryDirectory(prefix="filefinder_release_bundle_") as temp_dir:
        staging_root, managed_files = build_staging_tree(Path(temp_dir), version)
        manifest = {
            "version": version,
            "updater_api_version": UPDATER_API_VERSION,
            "min_current_version": args.min_current_version.strip().lstrip("v"),
            "sha256": payload_sha256(staging_root, managed_files),
            "archive_name": archive_name,
            "source_commit": source_commit(),
            "managed_files": managed_files,
            "notes": args.notes,
        }
        write_zip(staging_root, managed_files, archive_path, manifest_name, manifest)

    print(f"Wrote {archive_path}")
    print(f"Embedded {manifest_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
