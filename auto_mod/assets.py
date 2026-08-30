"""Archive access helpers for Auto Mod."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from filefinder.archive.idx_wpk import extract_matching_entries
from filefinder.core.paths import ParsedInput, discover_archives, parse_asset_path, resolve_thy_path
from filefinder.lookup.thy import ThyLookupTable


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
                f"No .idx archives were found in {game_root / 'res'} "
                f"or {game_root / 'Documents' / 'res'}"
            )
        self._thy_cache: dict[str, ThyLookupTable] = {}

    def parse(self, raw_path: str) -> ParsedInput:
        return parse_asset_path(raw_path, self.archives)

    def extract(self, raw_path: str) -> ExtractedAsset:
        request = self.parse(raw_path)
        lookup = self._lookup(request)
        remaining = {lookup.hash128_hex}
        for idx_path in request.archive.idx_paths:
            found = extract_matching_entries(idx_path, remaining)
            match = found.get(lookup.hash128_hex)
            if match is not None:
                return ExtractedAsset(request=request, data=match.data, source_archive=idx_path)
        raise FileNotFoundError(f"Asset was resolved in THY but not found in IDX/WPK: {raw_path}")

    def exists(self, raw_path: str) -> bool:
        try:
            request = self.parse(raw_path)
            lookup = self._lookup(request)
        except Exception:
            return False

        remaining = {lookup.hash128_hex}
        for idx_path in request.archive.idx_paths:
            if lookup.hash128_hex in extract_matching_entries(idx_path, remaining):
                return True
        return False

    def _lookup(self, request: ParsedInput):
        table = self._thy_cache.get(request.archive.stem)
        if table is None:
            table = ThyLookupTable(resolve_thy_path(self.game_root, request.archive.stem))
            self._thy_cache[request.archive.stem] = table
        return table.lookup(request.normalized_path)
