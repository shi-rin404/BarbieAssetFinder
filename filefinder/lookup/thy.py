"""THY path-to-Hash128 lookup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .flatbuffers import (
    BinaryReader,
    ParseError,
    PathNotFoundError,
    fb_field_offset,
    fb_required_pointer,
    read_u32_vector,
)
from .xxhash import MASK32, xxh32


@dataclass(frozen=True)
class LookupResult:
    original_path: str
    normalized_path: str
    primary_seed: int
    fallback_seed: int | None
    primary_key: int
    final_key: int
    used_fallback: bool
    descriptor_index: int
    hash128: bytes

    @property
    def hash128_hex(self) -> str:
        return self.hash128.hex()


class ThyLookupTable:
    """Read a THFB file and resolve normalized asset paths to 128-bit hashes."""

    THFB_HEADER_SIZE = 8
    TYPE_THX = 1
    TYPE_THXX = 11

    def __init__(
        self,
        thy_path: Path,
        *,
        validate_xxh32: bool = True,
        xxh32_seed: int = 0x163F,
    ):
        self.thy_path = thy_path
        self.data = thy_path.read_bytes()
        self.reader = BinaryReader(self.data, thy_path)
        self.xxh32_seed = xxh32_seed & MASK32
        self.variant_name = ""
        self.descriptor_stride = 0
        self.descriptor_hash_offset = 0
        self.descriptor_vector = 0
        self.descriptor_count = 0
        self.seeds: list[int] = []
        self.collision_keys: set[int] = set()
        self.hash_to_descriptor_index: dict[int, int] = {}
        self.stored_payload_xxh32 = 0
        self.calculated_payload_xxh32 = 0
        self.payload_xxh32_valid = False
        self._parse(validate_xxh32=validate_xxh32)

    def _parse(self, *, validate_xxh32: bool) -> None:
        reader = self.reader
        if reader.read(0, 4, "THFB magic") != b"THFB":
            raise ParseError(f"Invalid THY magic in {self.thy_path}")

        self.stored_payload_xxh32 = reader.u32(4, "THFB payload XXH32")
        self.calculated_payload_xxh32 = xxh32(
            self.data[self.THFB_HEADER_SIZE :],
            seed=self.xxh32_seed,
        )
        self.payload_xxh32_valid = (
            self.stored_payload_xxh32 == self.calculated_payload_xxh32
        )
        if validate_xxh32 and not self.payload_xxh32_valid:
            raise ParseError(
                "THY payload XXH32 validation failed: "
                f"stored=0x{self.stored_payload_xxh32:08X}, "
                f"calculated=0x{self.calculated_payload_xxh32:08X}"
            )

        flatbuffer_start = self.THFB_HEADER_SIZE
        outer_root = flatbuffer_start + reader.u32(
            flatbuffer_start,
            "outer FlatBuffer root offset",
        )
        reader.require(outer_root, 4, "outer FlatBuffer root")

        type_field_offset = fb_field_offset(reader, outer_root, 4)
        if type_field_offset == 0:
            raise ParseError("Outer FlatBuffer union type is missing")
        union_type = reader.u8(outer_root + type_field_offset, "outer union type")
        payload_root = fb_required_pointer(reader, outer_root, 6, "THX/THXX payload root")

        if union_type == self.TYPE_THXX:
            self.variant_name = "THXX"
            self.descriptor_stride = 0x20
            self.descriptor_hash_offset = 0x10
        elif union_type == self.TYPE_THX:
            self.variant_name = "THX"
            self.descriptor_stride = 0x18
            self.descriptor_hash_offset = 0x08
        else:
            raise ParseError(f"Unsupported THY union type: {union_type}")

        self.descriptor_vector = fb_required_pointer(
            reader,
            payload_root,
            4,
            "descriptor vector",
        )
        metadata_table = fb_required_pointer(
            reader,
            payload_root,
            6,
            "lookup metadata table",
        )
        final_hash_vector = fb_required_pointer(reader, metadata_table, 4, "final Hash32 vector")
        collision_vector = fb_required_pointer(reader, metadata_table, 6, "collision vector")
        seed_vector = fb_required_pointer(reader, metadata_table, 8, "seed vector")

        self.seeds = read_u32_vector(reader, seed_vector, "seed vector")
        collision_values = read_u32_vector(reader, collision_vector, "collision vector")
        final_hashes = read_u32_vector(reader, final_hash_vector, "final Hash32 vector")
        self.descriptor_count = reader.u32(self.descriptor_vector, "descriptor count")
        reader.require(
            self.descriptor_vector + 4,
            self.descriptor_count * self.descriptor_stride,
            "descriptor data",
        )

        if not self.seeds:
            raise ParseError("THY seed vector is empty")
        if self.descriptor_count != len(final_hashes):
            raise ParseError(
                f"Hash/descriptor count mismatch: hashes={len(final_hashes)}, "
                f"descriptors={self.descriptor_count}"
            )

        self.collision_keys = set(collision_values)
        if len(self.collision_keys) != len(collision_values):
            raise ParseError("Collision vector contains duplicate values")
        if self.collision_keys and len(self.seeds) < 2:
            raise ParseError("Collision vector exists but no fallback seed is present")

        for index, key in enumerate(final_hashes):
            if key in self.hash_to_descriptor_index:
                raise ParseError(f"Duplicate final Hash32 in THY table: 0x{key:08X}")
            self.hash_to_descriptor_index[key] = index

    @staticmethod
    def normalize_path(path: str) -> str:
        parts: list[str] = []
        for part in path.replace("\\", "/").strip("/").split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ValueError(f"Unsafe path segment is not allowed: {path!r}")
            parts.append(part)
        return "/".join(parts)

    def lookup(self, path: str) -> LookupResult:
        normalized = self.normalize_path(path)
        encoded = normalized.encode("utf-8")
        primary_seed = self.seeds[0]
        primary_key = xxh32(encoded, primary_seed)

        used_fallback = primary_key in self.collision_keys
        fallback_seed: int | None = None
        if used_fallback:
            fallback_seed = self.seeds[1]
            final_key = xxh32(encoded, fallback_seed)
        else:
            final_key = primary_key

        descriptor_index = self.hash_to_descriptor_index.get(final_key)
        if descriptor_index is None:
            detail = ""
            if len(self.seeds) >= 2:
                alternate_key = xxh32(encoded, self.seeds[1])
                alternate_index = self.hash_to_descriptor_index.get(alternate_key)
                detail = f"; alternate_key=0x{alternate_key:08X}, alternate_index={alternate_index}"
            raise PathNotFoundError(
                f"Path is not present in THY table: {normalized!r}; "
                f"primary=0x{primary_key:08X}, selected=0x{final_key:08X}{detail}"
            )

        descriptor = self.descriptor_vector + 4 + descriptor_index * self.descriptor_stride
        hash128 = self.reader.read(
            descriptor + self.descriptor_hash_offset,
            16,
            f"descriptor[{descriptor_index}].hash128",
        )
        return LookupResult(
            original_path=path,
            normalized_path=normalized,
            primary_seed=primary_seed,
            fallback_seed=fallback_seed,
            primary_key=primary_key,
            final_key=final_key,
            used_fallback=used_fallback,
            descriptor_index=descriptor_index,
            hash128=hash128,
        )

