"""Filtered IDX/WPK reader for extracting known Hash128 records."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .codecs import (
    EntryDataFlags,
    decode_slot_payload_auto,
    try_decode_payload_stage1,
    unwrap_nested_payloads,
)

LOG = logging.getLogger(__name__)

IDX_HEAD_SIZE = 0x20
IDX_REC_SIZE = 0x24
WPK_MAGIC = b"FKPW"
EMBEDDED_MAGIC = b"1DPW"
MIN_EMBEDDED_HEADER_SIZE = 0x30


@dataclass
class IndexEntry:
    filename: str = ""
    file_signature: int = 0
    lookup_key: int = 0
    file_offset: int = 0
    file_length: int = 0
    file_original_length: int = 0
    pkg_id: int = 0
    hdr_size: int = 0
    payload_size: int = 0
    raw_hash_hex: str = ""
    index: int = -1


@dataclass
class LoadedEntry(IndexEntry):
    data: bytes = b""
    raw_data: bytes = b""
    payload_data: bytes = b""
    source_mode: str = ""
    data_flags: EntryDataFlags = EntryDataFlags.NONE
    is_slot_file: bool = False
    stage1_decoded: bool = False
    stage1_tag: int | None = None
    stage1_skip_header_decode: bool = False
    unwrap_layers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CachedArchiveIndex:
    file_count: int
    entries_by_hash: dict[str, IndexEntry]


class ArchiveIndexCache:
    """Process-local IDX/WPK metadata cache keyed by archive path."""

    def __init__(self) -> None:
        self._cache: dict[Path, CachedArchiveIndex] = {}

    def get(self, archive_path: Path) -> CachedArchiveIndex:
        key = Path(archive_path).resolve(strict=False)
        cached = self._cache.get(key)
        if cached is None:
            cached = build_cached_archive_index(key)
            self._cache[key] = cached
        return cached

    def clear(self) -> None:
        self._cache.clear()


class IDXWPKArchive:
    """Open one IDX/WPK archive and extract selected entries by Hash128 hex."""

    def __init__(
        self,
        file_path: Path,
        *,
        target_hashes: set[str] | None = None,
        preloaded_indices: list[IndexEntry] | None = None,
        preloaded_file_count: int | None = None,
    ):
        self.file_path = Path(file_path)
        self.target_hashes = {value.lower() for value in target_hashes or set()}
        self.entries: dict[int, LoadedEntry] = {}
        self.indices: list[IndexEntry] = []
        self.file_count = 0
        self._wpk_cache: dict[int, BinaryIO | None] = {}
        self._wpk_paths: dict[int, str] = {}
        self._dir_index_cache: dict[Path, tuple[dict[str, Path], dict[str, list[Path]], list[Path]]] = {}

        self.mode = ""
        self.idx_path: Path | None = None
        self.wpk_path: Path | None = None
        self.base_dir = self.file_path.parent
        self.base_stem = self.file_path.stem

        with self.file_path.open("rb") as file:
            magic = file.read(4)

        if magic == b"SKPW":
            self.mode = "idx"
            self.idx_path = self.file_path
            if preloaded_indices is not None:
                self.indices = preloaded_indices
                self.file_count = preloaded_file_count or len(preloaded_indices)
            else:
                with self.file_path.open("rb") as file:
                    self._read_idx_header(file)
                    self._read_idx_indices(file)
        elif magic == WPK_MAGIC:
            self.mode = "wpk"
            self.wpk_path = self.file_path
            self._wpk_paths[0] = str(self.file_path)
            if preloaded_indices is not None:
                self.indices = preloaded_indices
                self.file_count = preloaded_file_count or len(preloaded_indices)
            else:
                with self.file_path.open("rb") as file:
                    self._read_wpk_header(file)
                    self._scan_wpk_indices(file)
        else:
            raise ValueError(f"Unsupported IDX/WPK file type: {self.file_path}")

    def close(self) -> None:
        for handle in self._wpk_cache.values():
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._wpk_cache.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _read_idx_header(self, file: BinaryIO) -> None:
        magic = file.read(4)
        if magic != b"SKPW":
            raise ValueError(f"Not a valid SKPW IDX file: {self.file_path}")
        file.seek(0x0C)
        self.file_count = int.from_bytes(file.read(4), "little")

    def _read_idx_indices(self, file: BinaryIO) -> None:
        self.indices = []
        remaining = set(self.target_hashes)
        file.seek(IDX_HEAD_SIZE)

        for index in range(self.file_count):
            record = file.read(IDX_REC_SIZE)
            if len(record) != IDX_REC_SIZE:
                raise EOFError(f"IDX truncated while reading record {index}: {self.file_path}")

            raw_hash = record[0x00:0x10]
            raw_hash_hex = raw_hash.hex()
            if remaining and raw_hash_hex not in remaining:
                continue

            lookup_key = int.from_bytes(record[0x10:0x14], "little")
            pkg_id = record[0x14]
            file_offset = int.from_bytes(record[0x18:0x1C], "little")
            payload_size = int.from_bytes(record[0x1C:0x20], "little")
            hdr_size = int.from_bytes(record[0x20:0x22], "little")
            total_size = hdr_size + payload_size

            self.indices.append(
                IndexEntry(
                    file_signature=int.from_bytes(raw_hash, "little"),
                    lookup_key=lookup_key,
                    file_offset=file_offset,
                    file_length=total_size,
                    file_original_length=total_size,
                    filename=raw_hash_hex,
                    pkg_id=pkg_id,
                    hdr_size=hdr_size,
                    payload_size=payload_size,
                    raw_hash_hex=raw_hash_hex,
                    index=index,
                )
            )

            if remaining:
                remaining.discard(raw_hash_hex)
                if not remaining:
                    break

    def _read_wpk_header(self, file: BinaryIO) -> None:
        magic = file.read(4)
        if magic != WPK_MAGIC:
            raise ValueError(f"Not a valid FKPW WPK file: {self.file_path}")

    def _scan_wpk_indices(self, file: BinaryIO) -> None:
        data = file.read()
        offsets: list[int] = []
        pos = 0
        while True:
            found = data.find(EMBEDDED_MAGIC, pos)
            if found == -1:
                break
            offsets.append(found)
            pos = found + 1

        remaining = set(self.target_hashes)
        self.indices = []
        for ordinal, offset in enumerate(offsets):
            next_offset = offsets[ordinal + 1] if ordinal + 1 < len(offsets) else len(data)
            entry = build_index_from_embedded_header(data, offset, next_offset, ordinal)
            if remaining and entry.raw_hash_hex not in remaining:
                continue
            self.indices.append(entry)
            if remaining:
                remaining.discard(entry.raw_hash_hex)
                if not remaining:
                    break

        self.file_count = len(offsets)
        self._wpk_paths[0] = str(self.file_path)

    def iter_wpk_path_candidates(self, pkg_id: int):
        seen: set[str] = set()

        def push(candidate: Path):
            normalized = os.path.normpath(str(candidate))
            if normalized in seen:
                return
            seen.add(normalized)
            yield normalized

        custom = self._wpk_paths.get(pkg_id)
        if custom:
            yield from push(Path(custom))

        stem_pattern = re.escape(self.base_stem)
        regex = re.compile(rf"^{stem_pattern}_?0*{int(pkg_id)}\.wpk$", re.IGNORECASE)
        yield from push(self.base_dir / f"{self.base_stem}{pkg_id}.wpk")
        yield from push(self.base_dir / f"{self.base_stem}_{pkg_id}.wpk")

        try:
            for name in sorted(os.listdir(self.base_dir)):
                if regex.fullmatch(name):
                    yield from push(self.base_dir / name)
        except OSError:
            return

    def find_wpk_path(self, pkg_id: int) -> str:
        if self.mode == "wpk":
            return str(self.file_path)
        for candidate in self.iter_wpk_path_candidates(pkg_id):
            if os.path.exists(candidate):
                self._wpk_paths[pkg_id] = candidate
                return candidate
        return str(self.base_dir / f"{self.base_stem}{pkg_id}.wpk")

    @staticmethod
    def is_slot_file_pkg(pkg_id: int) -> bool:
        return not (0 <= int(pkg_id) <= 15)

    def get_slot_file_dir(self) -> Path:
        slot_name = re.sub(r"\d+$", "", self.base_stem) or self.base_stem
        return self.base_dir / slot_name

    def get_wpk_handle(self, pkg_id: int) -> BinaryIO | None:
        if pkg_id in self._wpk_cache:
            return self._wpk_cache[pkg_id]

        wpk_path = self.find_wpk_path(pkg_id)
        if not os.path.exists(wpk_path):
            LOG.warning("WPK not found for pkg_id=%d: %s", pkg_id, wpk_path)
            self._wpk_cache[pkg_id] = None
            return None

        handle = open(wpk_path, "rb")
        self._wpk_cache[pkg_id] = handle
        return handle

    def read_slot_file_data(self, entry: LoadedEntry) -> bytes | None:
        slot_dir = self.get_slot_file_dir()
        if not slot_dir.is_dir():
            return None

        exact_name, exact_stem, ordered_files = self._get_dir_index(slot_dir)
        raw_hash_hex = entry.raw_hash_hex or entry.filename.split(".", 1)[0]
        candidate_paths: list[Path] = []
        for key in (raw_hash_hex, entry.filename):
            path = exact_name.get(key)
            if path is not None:
                candidate_paths.append(path)
        candidate_paths.extend(exact_stem.get(raw_hash_hex, []))

        seen: set[str] = set()
        for candidate in candidate_paths:
            candidate_text = str(candidate)
            if candidate_text in seen:
                continue
            seen.add(candidate_text)
            if candidate.is_file():
                return candidate.read_bytes()

        for path in ordered_files:
            if path.stem.startswith(raw_hash_hex):
                return path.read_bytes()
        return None

    def _get_dir_index(self, slot_dir: Path):
        cached = self._dir_index_cache.get(slot_dir)
        if cached is not None:
            return cached

        exact_name: dict[str, Path] = {}
        exact_stem: dict[str, list[Path]] = {}
        ordered_files: list[Path] = []
        for path in sorted(slot_dir.iterdir()):
            if not path.is_file():
                continue
            ordered_files.append(path)
            exact_name.setdefault(path.name, path)
            exact_stem.setdefault(path.stem, []).append(path)

        cached = (exact_name, exact_stem, ordered_files)
        self._dir_index_cache[slot_dir] = cached
        return cached

    def load_entry(self, entry: IndexEntry) -> LoadedEntry:
        loaded = LoadedEntry(**entry.__dict__)
        loaded.source_mode = self.mode
        self._load_entry_data(loaded)
        self.entries[entry.index] = loaded
        return loaded

    def _load_entry_data(self, entry: LoadedEntry) -> None:
        pkg_id = entry.pkg_id

        if self.mode == "idx" and self.is_slot_file_pkg(pkg_id) and not self.has_wpk_for_pkg(pkg_id):
            raw_data = self.read_slot_file_data(entry)
            if raw_data is None:
                raise FileNotFoundError(
                    f"Missing slot_file for pkg_id={pkg_id} in {self.get_slot_file_dir()}"
                )
            entry.is_slot_file = True
            entry.source_mode = "slot_file"
            entry.file_length = len(raw_data)
            entry.file_original_length = len(raw_data)
            if raw_data[:4] == EMBEDDED_MAGIC:
                payload = (
                    raw_data[entry.hdr_size : entry.hdr_size + entry.payload_size]
                    if entry.payload_size > 0
                    else raw_data[entry.hdr_size :]
                )
            else:
                payload = raw_data
        else:
            handle = self.get_wpk_handle(pkg_id)
            if handle is None:
                raise FileNotFoundError(f"Missing WPK for pkg_id={pkg_id}")

            total_size = entry.file_length if entry.file_length > 0 else entry.hdr_size + entry.payload_size
            handle.seek(entry.file_offset)
            raw_data = handle.read(total_size)
            if len(raw_data) != total_size:
                raise EOFError(
                    f"Failed to read entry data: expected {total_size}, got {len(raw_data)}"
                )

            if entry.hdr_size > 0 and entry.hdr_size <= len(raw_data):
                payload = (
                    raw_data[entry.hdr_size : entry.hdr_size + entry.payload_size]
                    if entry.payload_size > 0
                    else raw_data[entry.hdr_size :]
                )
            else:
                payload = raw_data

        entry.raw_data = raw_data
        entry.payload_data = payload

        if entry.is_slot_file:
            processed, decoded, tag, used_skip_header_decode = decode_slot_payload_auto(
                payload,
                context=f"{entry.filename} pkg={pkg_id} source={entry.source_mode}",
            )
            entry.data = processed
            entry.stage1_decoded = decoded
            entry.stage1_tag = tag
            entry.stage1_skip_header_decode = used_skip_header_decode
        else:
            processed, decoded, tag = try_decode_payload_stage1(
                payload,
                context=f"{entry.filename} pkg={pkg_id} source={entry.source_mode}",
                skip_header_decode=False,
            )
            entry.data = processed
            entry.stage1_decoded = decoded
            entry.stage1_tag = tag

        entry.data, entry.unwrap_layers, extra_flags = unwrap_nested_payloads(
            entry.data,
            context=f"{entry.filename} pkg={pkg_id} source={entry.source_mode}",
        )
        entry.data_flags |= extra_flags

        entry.file_length = len(entry.data)
        entry.file_original_length = len(entry.data)

    def has_wpk_for_pkg(self, pkg_id: int) -> bool:
        return any(os.path.exists(candidate) for candidate in self.iter_wpk_path_candidates(pkg_id))


def build_index_from_embedded_header(
    data: bytes,
    offset: int,
    next_offset: int,
    ordinal: int,
) -> IndexEntry:
    hdr_size = 0
    payload_size = 0
    raw_hash = b""
    lookup_key = 0

    available = len(data) - offset
    if available >= MIN_EMBEDDED_HEADER_SIZE:
        raw_hash = data[offset + 0x08 : offset + 0x18]
        lookup_key = int.from_bytes(data[offset + 0x18 : offset + 0x1C], "little")
        payload_size = int.from_bytes(data[offset + 0x20 : offset + 0x24], "little")
        hdr_size = int.from_bytes(data[offset + 0x24 : offset + 0x26], "little")

    guessed_total = hdr_size + payload_size if hdr_size > 0 else 0
    max_possible = max(0, next_offset - offset)
    if hdr_size < MIN_EMBEDDED_HEADER_SIZE or hdr_size > max_possible:
        hdr_size = MIN_EMBEDDED_HEADER_SIZE if max_possible >= MIN_EMBEDDED_HEADER_SIZE else max_possible
    if guessed_total <= 0 or guessed_total > max_possible:
        total_size = max_possible
        if total_size >= hdr_size:
            payload_size = total_size - hdr_size
        else:
            hdr_size = total_size
            payload_size = 0
    else:
        total_size = guessed_total

    if not raw_hash or raw_hash == b"\x00" * 16:
        raw_hash = offset.to_bytes(8, "little") + ordinal.to_bytes(8, "little")

    raw_hash_hex = raw_hash.hex()
    return IndexEntry(
        file_signature=int.from_bytes(raw_hash, "little"),
        lookup_key=lookup_key,
        file_offset=offset,
        file_length=total_size,
        file_original_length=total_size,
        filename=raw_hash_hex,
        pkg_id=0,
        hdr_size=hdr_size,
        payload_size=payload_size,
        raw_hash_hex=raw_hash_hex,
        index=ordinal,
    )


def extract_matching_entries(
    archive_path: Path,
    target_hashes: set[str],
    *,
    index_cache: ArchiveIndexCache | None = None,
) -> dict[str, LoadedEntry]:
    """Return decoded entries whose raw hash is in target_hashes."""
    target_hashes = {value.lower() for value in target_hashes}
    found: dict[str, LoadedEntry] = {}
    if index_cache is not None:
        cached = index_cache.get(archive_path)
        indices = [
            entry
            for entry in cached.entries_by_hash.values()
            if entry.raw_hash_hex in target_hashes
        ]
        with IDXWPKArchive(
            archive_path,
            preloaded_indices=indices,
            preloaded_file_count=cached.file_count,
        ) as archive:
            for index_entry in archive.indices:
                if index_entry.raw_hash_hex in found:
                    continue
                found[index_entry.raw_hash_hex] = archive.load_entry(index_entry)
        return found

    with IDXWPKArchive(archive_path, target_hashes=target_hashes) as archive:
        for index_entry in archive.indices:
            if index_entry.raw_hash_hex in found:
                continue
            found[index_entry.raw_hash_hex] = archive.load_entry(index_entry)
    return found


def build_cached_archive_index(archive_path: Path) -> CachedArchiveIndex:
    with IDXWPKArchive(archive_path) as archive:
        entries_by_hash: dict[str, IndexEntry] = {}
        for entry in archive.indices:
            if entry.raw_hash_hex:
                entries_by_hash.setdefault(entry.raw_hash_hex, entry)
        return CachedArchiveIndex(
            file_count=archive.file_count,
            entries_by_hash=entries_by_hash,
        )
