"""Self-update helpers for FileFinderV2 release bundles."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import py_compile
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from filefinder.version import UPDATER_API_VERSION, __version__


GITHUB_REPO = "shi-rin404/BarbieAssetFinder"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = "FileFinderV2-Updater"
FILEFINDER_ASSET_PREFIX = "filefinder-"
RESTART_ENV_VAR = "FILEFINDER_RESTARTED_AFTER_UPDATE"
USER_DIR_NAME = "user"
INSTALLED_MANIFEST_PATH = Path(__file__).resolve().parents[2] / USER_DIR_NAME / "self_update_manifest.json"


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str


@dataclass(frozen=True)
class UpdateManifest:
    version: tuple[int, ...]
    updater_api_version: int
    min_current_version: tuple[int, ...]
    sha256: str
    archive_name: str
    source_commit: str
    managed_files: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class UpdateCheckResult:
    manifest: UpdateManifest | None
    available: bool
    current_version: str
    latest_version: str
    status: str


@dataclass(frozen=True)
class UpdateInstallResult:
    manifest: UpdateManifest
    installed: bool
    status: str


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_version(value) -> tuple[int, ...]:
    if isinstance(value, (tuple, list)):
        return tuple(int(part) for part in value)

    text = str(value).strip()
    if text.startswith(("v", "V")):
        text = text[1:]

    parts: list[int] = []
    for part in text.replace("-", ".").split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    return tuple(parts or [0])


def format_version(version: tuple[int, ...]) -> str:
    return "v" + ".".join(str(part) for part in version)


def current_version() -> tuple[int, ...]:
    return parse_version(__version__)


def _request_bytes(url: str, *, timeout: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _request_json(url: str) -> dict:
    return json.loads(_request_bytes(url, timeout=20).decode("utf-8"))


def _download_file(url: str, target_path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        with target_path.open("wb") as output:
            shutil.copyfileobj(response, output)


def _release_assets(release: dict) -> list[ReleaseAsset]:
    assets: list[ReleaseAsset] = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if name and url:
            assets.append(ReleaseAsset(name=name, url=url))
    return assets


def _select_manifest_asset(assets: list[ReleaseAsset]) -> ReleaseAsset:
    candidates = [
        asset
        for asset in assets
        if asset.name.lower().startswith(FILEFINDER_ASSET_PREFIX)
        and asset.name.lower().endswith(".json")
    ]
    if not candidates:
        raise ValueError("Latest release does not include a FileFinderV2 manifest")
    return sorted(candidates, key=lambda item: item.name)[-1]


def _select_archive_asset(assets: list[ReleaseAsset], manifest: UpdateManifest) -> ReleaseAsset:
    by_name = {asset.name: asset for asset in assets}
    if manifest.archive_name in by_name:
        return by_name[manifest.archive_name]

    candidates = [
        asset
        for asset in assets
        if asset.name.lower().startswith(FILEFINDER_ASSET_PREFIX)
        and asset.name.lower().endswith(".zip")
    ]
    if not candidates:
        raise ValueError("Latest release does not include a FileFinderV2 zip")
    return sorted(candidates, key=lambda item: item.name)[-1]


def _normalize_managed_path(value: str) -> str:
    path = value.strip().replace("\\", "/").strip("/")
    if not path or path.startswith("../") or "/../" in f"/{path}/" or path.startswith("/"):
        raise ValueError(f"Unsafe managed file path: {value!r}")
    parts = set(path.split("/"))
    blocked = {".git", USER_DIR_NAME, "outputs", "tmp", "dist", "__pycache__"}
    if parts & blocked:
        raise ValueError(f"Managed file path is not allowed: {path}")
    return path


def parse_manifest(data: bytes) -> UpdateManifest:
    raw = json.loads(data.decode("utf-8"))
    required = (
        "version",
        "updater_api_version",
        "min_current_version",
        "sha256",
        "archive_name",
        "managed_files",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"FileFinderV2 manifest is missing: {', '.join(missing)}")

    managed_files = raw["managed_files"]
    if not isinstance(managed_files, list):
        raise ValueError("Manifest managed_files must be a list")

    normalized_files = tuple(sorted({_normalize_managed_path(str(path)) for path in managed_files}))
    if not normalized_files:
        raise ValueError("Manifest does not include any managed files")

    return UpdateManifest(
        version=parse_version(raw["version"]),
        updater_api_version=int(raw["updater_api_version"]),
        min_current_version=parse_version(raw["min_current_version"]),
        sha256=str(raw["sha256"]).strip().lower(),
        archive_name=str(raw["archive_name"]).strip(),
        source_commit=str(raw.get("source_commit", "")).strip(),
        managed_files=normalized_files,
        notes=str(raw.get("notes", "")).strip(),
    )


def fetch_latest_release() -> tuple[UpdateManifest, ReleaseAsset]:
    release = _request_json(LATEST_RELEASE_API)
    assets = _release_assets(release)
    manifest_asset = _select_manifest_asset(assets)
    manifest = parse_manifest(_request_bytes(manifest_asset.url, timeout=20))
    archive_asset = _select_archive_asset(assets, manifest)
    return manifest, archive_asset


def validate_manifest_compatibility(manifest: UpdateManifest) -> None:
    if manifest.updater_api_version != UPDATER_API_VERSION:
        raise ValueError(
            f"Unsupported updater API version: {manifest.updater_api_version}; "
            f"expected {UPDATER_API_VERSION}"
        )
    if current_version() < manifest.min_current_version:
        raise ValueError(
            f"Update {format_version(manifest.version)} requires "
            f"FileFinderV2 {format_version(manifest.min_current_version)} or newer"
        )


def check_for_update() -> UpdateCheckResult:
    manifest, _asset = fetch_latest_release()
    validate_manifest_compatibility(manifest)
    available = manifest.version > current_version()
    current_text = format_version(current_version())
    latest_text = format_version(manifest.version)
    status = (
        f"Update available: {current_text} -> {latest_text}"
        if available
        else f"Already up to date: {current_text}"
    )
    return UpdateCheckResult(
        manifest=manifest,
        available=available,
        current_version=current_text,
        latest_version=latest_text,
        status=status,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract_zip(archive_path: Path, extract_root: Path) -> None:
    extract_root.mkdir(parents=True, exist_ok=True)
    resolved_root = extract_root.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (extract_root / member.filename).resolve()
            try:
                target.relative_to(resolved_root)
            except ValueError:
                raise ValueError(f"Unsafe path in update zip: {member.filename}")
        archive.extractall(extract_root)


def _zip_file_set(source_root: Path) -> set[str]:
    return {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }


def validate_extracted_update(source_root: Path, manifest: UpdateManifest) -> None:
    zip_files = _zip_file_set(source_root)
    managed_files = set(manifest.managed_files)
    missing = managed_files - zip_files
    extra = zip_files - managed_files
    if missing:
        raise ValueError(f"Update zip is missing managed files: {', '.join(sorted(missing)[:8])}")
    if extra:
        raise ValueError(f"Update zip includes unmanaged files: {', '.join(sorted(extra)[:8])}")

    for relative in manifest.managed_files:
        if relative.endswith(".py"):
            py_compile.compile(str(source_root / relative), doraise=True)

    version_path = source_root / "filefinder" / "version.py"
    if not version_path.is_file():
        raise ValueError("Update zip does not include filefinder/version.py")

    module_name = "_filefinder_update_smoke_version"
    spec = importlib.util.spec_from_file_location(module_name, version_path)
    if spec is None or spec.loader is None:
        raise ValueError("Could not import update version file")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    if parse_version(getattr(module, "__version__", "0.0.0")) != manifest.version:
        raise ValueError("Update package version does not match manifest")
    if int(getattr(module, "UPDATER_API_VERSION", 0)) != manifest.updater_api_version:
        raise ValueError("Update package updater API version does not match manifest")


def load_installed_manifest() -> dict | None:
    if not INSTALLED_MANIFEST_PATH.is_file():
        return None
    try:
        return json.loads(INSTALLED_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_installed_manifest(manifest: UpdateManifest) -> None:
    INSTALLED_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": format_version(manifest.version),
        "updater_api_version": manifest.updater_api_version,
        "managed_files": list(manifest.managed_files),
        "source_commit": manifest.source_commit,
    }
    INSTALLED_MANIFEST_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def previous_managed_files() -> set[str]:
    manifest = load_installed_manifest()
    if not manifest:
        return set()
    managed_files = manifest.get("managed_files", [])
    if not isinstance(managed_files, list):
        return set()
    result: set[str] = set()
    for value in managed_files:
        try:
            result.add(_normalize_managed_path(str(value)))
        except ValueError:
            continue
    return result


def backup_managed_files(managed_files: set[str], backup_root: Path) -> None:
    root = project_root()
    for relative in sorted(managed_files):
        source = root / relative
        if not source.is_file():
            continue
        target = backup_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def restore_backup(backup_root: Path, managed_files: set[str]) -> None:
    root = project_root()
    for relative in sorted(managed_files):
        target = root / relative
        backup = backup_root / relative
        if backup.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        elif target.exists():
            target.unlink()


def apply_update(source_root: Path, manifest: UpdateManifest) -> None:
    root = project_root().resolve()
    new_files = set(manifest.managed_files)
    old_files = previous_managed_files()
    touched_files = new_files | old_files

    with tempfile.TemporaryDirectory(prefix="filefinder_update_backup_") as temp_dir:
        backup_root = Path(temp_dir)
        backup_managed_files(touched_files, backup_root)
        try:
            for relative in sorted(new_files):
                source = source_root / relative
                target = (root / relative).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    raise ValueError(f"Refusing to update outside project root: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            for relative in sorted(old_files - new_files):
                target = (root / relative).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    raise ValueError(f"Refusing to remove outside project root: {target}")
                if target.is_file():
                    target.unlink()

            save_installed_manifest(manifest)
        except Exception:
            restore_backup(backup_root, touched_files)
            raise


def install_latest_update() -> UpdateInstallResult:
    manifest, archive_asset = fetch_latest_release()
    validate_manifest_compatibility(manifest)
    if manifest.version <= current_version():
        return UpdateInstallResult(
            manifest=manifest,
            installed=False,
            status=f"Already up to date: {format_version(current_version())}",
        )

    with tempfile.TemporaryDirectory(prefix="filefinder_update_") as temp_dir:
        temp_root = Path(temp_dir)
        archive_path = temp_root / manifest.archive_name
        extract_root = temp_root / "extract"
        _download_file(archive_asset.url, archive_path)
        actual_sha256 = sha256(archive_path)
        if actual_sha256 != manifest.sha256:
            raise ValueError(
                f"Update checksum mismatch: expected {manifest.sha256}, got {actual_sha256}"
            )
        safe_extract_zip(archive_path, extract_root)
        validate_extracted_update(extract_root, manifest)
        apply_update(extract_root, manifest)

    return UpdateInstallResult(
        manifest=manifest,
        installed=True,
        status=f"Installed FileFinderV2 {format_version(manifest.version)}",
    )


def restarted_after_update() -> bool:
    return os.environ.get(RESTART_ENV_VAR) == "1"


def restart_environment() -> dict[str, str]:
    env = os.environ.copy()
    env[RESTART_ENV_VAR] = "1"
    return env
