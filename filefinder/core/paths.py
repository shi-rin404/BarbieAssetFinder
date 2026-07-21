"""Game path discovery and input normalization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from filefinder.lookup.thy import ThyLookupTable


@dataclass(frozen=True)
class ArchiveSource:
    stem: str
    prefix: str
    res_idx: Path
    documents_res_idx: Path

    @property
    def idx_paths(self) -> tuple[Path, Path]:
        return (self.documents_res_idx, self.res_idx)


@dataclass(frozen=True)
class ParsedInput:
    raw_path: str
    archive: ArchiveSource
    normalized_path: str


def archive_prefix_from_stem(stem: str) -> str:
    return ThyLookupTable.normalize_path(stem.replace("_", "/"))


def discover_archives(game_root: Path) -> dict[str, ArchiveSource]:
    """Discover IDX names that exist in both res and Documents/res."""
    game_root = game_root.resolve()
    res_dir = game_root / "res"
    documents_res_dir = game_root / "Documents" / "res"

    res_names = {path.name for path in res_dir.glob("*.idx")} if res_dir.is_dir() else set()
    documents_names = (
        {path.name for path in documents_res_dir.glob("*.idx")}
        if documents_res_dir.is_dir()
        else set()
    )
    common_names = res_names & documents_names
    archives: dict[str, ArchiveSource] = {}

    for name in sorted(common_names):
        stem = Path(name).stem
        prefix = archive_prefix_from_stem(stem)
        archives[prefix] = ArchiveSource(
            stem=stem,
            prefix=prefix,
            res_idx=res_dir / name,
            documents_res_idx=documents_res_dir / name,
        )
    return archives


def parse_asset_path(raw_path: str, archives: dict[str, ArchiveSource]) -> ParsedInput:
    normalized = ThyLookupTable.normalize_path(raw_path)
    matches = [
        prefix
        for prefix in archives
        if normalized == prefix or normalized.startswith(prefix + "/")
    ]
    if not matches:
        fallback = parse_asset_path_by_root_archive(raw_path, normalized, archives)
        if fallback is not None:
            return fallback
        raise_no_archive_prefix(raw_path, archives)

    prefix = max(matches, key=len)
    stripped = normalized[len(prefix) :].strip("/")
    if not stripped:
        raise ValueError(f"Input path does not include an asset path after prefix: {raw_path!r}")

    return ParsedInput(
        raw_path=raw_path,
        archive=archives[prefix],
        normalized_path=ThyLookupTable.normalize_path(stripped),
    )


def parse_asset_path_by_root_archive(
    raw_path: str,
    normalized: str,
    archives: dict[str, ArchiveSource],
) -> ParsedInput | None:
    root_name, separator, stripped = normalized.partition("/")
    if not separator or not root_name or not stripped:
        return None

    archive_by_stem = {archive.stem: archive for archive in archives.values()}
    archive = archive_by_stem.get(root_name)
    if archive is None:
        return None

    return ParsedInput(
        raw_path=raw_path,
        archive=archive,
        normalized_path=ThyLookupTable.normalize_path(stripped),
    )


def raise_no_archive_prefix(raw_path: str, archives: dict[str, ArchiveSource]) -> None:
    available = ", ".join(sorted(archives)[:20])
    suffix = " ..." if len(archives) > 20 else ""
    raise ValueError(
        f"No archive prefix matched {raw_path!r}. Available prefixes: {available}{suffix}"
    )


def resolve_thy_path(game_root: Path, archive_stem: str) -> Path:
    candidates = (
        game_root / "Documents" / "thd" / f"{archive_stem}.thy",
        game_root / "thd" / f"{archive_stem}.thy",
        game_root / "Documents" / "res" / f"{archive_stem}.thy",
        game_root / "res" / f"{archive_stem}.thy",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise FileNotFoundError(f"THY file not found for archive {archive_stem!r}:\n{searched}")


def output_path_for(output_root: Path, prefix: str, normalized_path: str) -> Path:
    parts = [part for part in (prefix + "/" + normalized_path).split("/") if part]
    return output_root.joinpath(*parts)
