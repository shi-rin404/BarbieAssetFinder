"""BPSE binary JSON reader and writer.

The BPSE format stores dictionary keys as integer key indices. Use ``key_names``
when reading or ``key_indices`` when writing if those indices should be mapped
to human readable strings.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import struct
from typing import Any


BPSE_HEADER_SIZE = 0x10
BPSE_REFERENCE_MARKER = "__bpse_reference__"
BPSE_STRING_TABLE_ROOT_KEY = "0"
BPSE_OBJECT_ROOT_KEY = "1"


JsonValue = None | bool | int | float | str | list[Any] | dict[str, Any]


@dataclass(frozen=True)
class BPSEReference:
    """Runtime type 6 value whose exact game-side semantics are unknown."""

    value: int


@dataclass(frozen=True)
class BPSEDocument:
    magic: bytes
    unk0: int
    unk1: int
    unk2: int
    root: Any


class BPSEError(ValueError):
    """Raised when BPSE data cannot be parsed or serialized."""


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def read(self, size: int, context: str) -> bytes:
        if size < 0:
            raise BPSEError(f"Negative read size for {context}: {size}")
        end = self.offset + size
        if end > len(self.data):
            raise EOFError(f"Unexpected end of BPSE data while reading {context}")
        value = self.data[self.offset : end]
        self.offset = end
        return value

    def u8(self, context: str) -> int:
        return self.read(1, context)[0]

    def uint(self, width: int, context: str) -> int:
        return int.from_bytes(self.read(width, context), "little")

    def f32(self, context: str) -> float:
        return struct.unpack("<f", self.read(4, context))[0]

    def f64(self, context: str) -> float:
        return struct.unpack("<d", self.read(8, context))[0]


def from_bytes(
    data: bytes,
    *,
    key_names: list[str] | tuple[str, ...] | dict[int, str] | None = None,
    require_root_dict: bool = True,
    resolve_string_table: bool = True,
) -> BPSEDocument:
    """Parse a BPSE byte payload into a document."""
    if len(data) < BPSE_HEADER_SIZE:
        raise EOFError("BPSE data is shorter than the 16-byte header")

    reader = _Reader(data)
    magic = reader.read(8, "magic")
    unk0 = reader.uint(2, "unk0")
    unk1 = reader.uint(2, "unk1")
    unk2 = reader.uint(4, "unk2")
    parse_key_names = None if resolve_string_table else key_names
    root = _read_value(reader, has_key=False, key_names=parse_key_names)

    if require_root_dict and not isinstance(root, dict):
        raise BPSEError("BPSE payload root is not a dictionary")
    if reader.offset != len(data):
        raise BPSEError(
            f"BPSE data has {len(data) - reader.offset} trailing byte(s)"
        )
    if resolve_string_table:
        root = resolve_string_table_keys(root)
    return BPSEDocument(magic=magic, unk0=unk0, unk1=unk1, unk2=unk2, root=root)


def to_bytes(
    document: BPSEDocument | Any,
    *,
    key_indices: dict[str, int] | None = None,
    magic: bytes = b"BPSE\x00\x00\x00\x00",
    unk0: int = 0,
    unk1: int = 0,
    unk2: int = 0,
) -> bytes:
    """Serialize a document or root value into BPSE bytes."""
    if isinstance(document, BPSEDocument):
        magic = document.magic
        unk0 = document.unk0
        unk1 = document.unk1
        unk2 = document.unk2
        root = document.root
    else:
        root = document

    if len(magic) != 8:
        raise BPSEError("BPSE magic must be exactly 8 bytes")
    output = bytearray()
    output += magic
    output += _uint_bytes(unk0, 2, "unk0")
    output += _uint_bytes(unk1, 2, "unk1")
    output += _uint_bytes(unk2, 4, "unk2")
    output += _write_value(root, has_key=False, key_indices=key_indices or {})
    return bytes(output)


def loads(
    data: bytes,
    *,
    key_names: list[str] | tuple[str, ...] | dict[int, str] | None = None,
    include_header: bool = True,
    indent: int | None = 4,
    resolve_string_table: bool = True,
) -> str:
    """Return a JSON string from BPSE bytes."""
    document = from_bytes(
        data,
        key_names=key_names,
        resolve_string_table=resolve_string_table,
    )
    payload: JsonValue
    if include_header:
        payload = {
            "magic": document.magic.hex(),
            "unk0": document.unk0,
            "unk1": document.unk1,
            "unk2": document.unk2,
            "root": _to_json_value(document.root),
        }
    else:
        payload = _to_json_value(document.root)
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def dumps(
    text: str,
    *,
    key_indices: dict[str, int] | None = None,
    magic: bytes = b"BPSE\x00\x00\x00\x00",
    unk0: int = 0,
    unk1: int = 0,
    unk2: int = 0,
) -> bytes:
    """Return BPSE bytes from a JSON string.

    The JSON can either be a wrapper object with ``magic``, ``unk0``, ``unk1``,
    ``unk2`` and ``root`` fields, or a root value directly.
    """
    value = json.loads(text)
    if _looks_like_document_json(value):
        magic = bytes.fromhex(value["magic"])
        unk0 = int(value.get("unk0", 0))
        unk1 = int(value.get("unk1", 0))
        unk2 = int(value.get("unk2", 0))
        root = _from_json_value(value["root"])
        return to_bytes(
            BPSEDocument(magic=magic, unk0=unk0, unk1=unk1, unk2=unk2, root=root),
            key_indices=key_indices,
        )
    return to_bytes(
        _from_json_value(value),
        key_indices=key_indices,
        magic=magic,
        unk0=unk0,
        unk1=unk1,
        unk2=unk2,
    )


def resolve_string_table_keys(root: Any) -> Any:
    """Resolve NeoX BPSE root key table indices to string dictionary keys.

    NeoX BPSE payloads commonly store a tagged root dictionary where key ``0``
    is the key string table and key ``1`` is the actual serialized object root.
    When that shape is present, this returns the object root with every numeric
    dictionary key replaced by the corresponding string table value.
    """
    if not isinstance(root, dict):
        return root

    table = root.get(BPSE_STRING_TABLE_ROOT_KEY)
    object_root = root.get(BPSE_OBJECT_ROOT_KEY)
    if not _is_string_table(table) or object_root is None:
        return root
    return _resolve_value_keys(object_root, table)


def _read_value(
    reader: _Reader,
    *,
    has_key: bool,
    key_names: list[str] | tuple[str, ...] | dict[int, str] | None,
) -> Any:
    if has_key:
        _read_key_index(reader)

    type_and_data = reader.u8("value tag")
    return _read_value_body(reader, type_and_data, key_names=key_names)


def _read_entry(
    reader: _Reader,
    *,
    key_names: list[str] | tuple[str, ...] | dict[int, str] | None,
) -> tuple[str, Any]:
    key_index = _read_key_index(reader)
    key = _key_name(key_index, key_names)
    type_and_data = reader.u8("dictionary value tag")
    return key, _read_value_body(reader, type_and_data, key_names=key_names)


def _read_value_body(
    reader: _Reader,
    type_and_data: int,
    *,
    key_names: list[str] | tuple[str, ...] | dict[int, str] | None,
) -> Any:
    storage_class = type_and_data & 0xC0
    extended_type = type_and_data & 0x0F
    width = 1 << ((type_and_data >> 4) & 0x03)

    if storage_class == 0x40:
        length = type_and_data & 0x3F
        return reader.read(length, "compact string").decode("utf-8")

    if storage_class == 0x80:
        return _decode_zigzag(type_and_data & 0x3F)

    if storage_class == 0xC0:
        entry_count = type_and_data & 0x3F
        return _read_dict(reader, entry_count, key_names=key_names)

    if type_and_data == 0x10:
        return None
    if type_and_data == 0x20:
        return False
    if type_and_data == 0x30:
        return True

    if extended_type == 0x01:
        return _decode_zigzag(reader.uint(width, "zigzag integer"))
    if extended_type == 0x02:
        return reader.uint(width, "unsigned integer")
    if extended_type == 0x03:
        if width == 4:
            return reader.f32("float32")
        if width == 8:
            return reader.f64("float64")
        raise BPSEError(f"Invalid BPSE floating-point width: {width}")
    if extended_type == 0x04:
        length = reader.uint(width, "string length")
        return reader.read(length, "string").decode("utf-8")
    if extended_type == 0x08:
        element_count = reader.uint(width, "list element count")
        return [
            _read_value(reader, has_key=False, key_names=key_names)
            for _ in range(element_count)
        ]
    if extended_type == 0x09:
        entry_count = reader.uint(width, "dictionary entry count")
        return _read_dict(reader, entry_count, key_names=key_names)
    if extended_type == 0x0A:
        return BPSEReference(reader.uint(width, "reference value"))

    raise BPSEError(f"Unknown BPSE value tag: 0x{type_and_data:02X}")


def _read_dict(
    reader: _Reader,
    entry_count: int,
    *,
    key_names: list[str] | tuple[str, ...] | dict[int, str] | None,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for _ in range(entry_count):
        key, value = _read_entry(reader, key_names=key_names)
        values[key] = value
    return values


def _read_key_index(reader: _Reader) -> int:
    prefix = reader.u8("key index prefix")
    if (prefix & 0x80) == 0:
        return prefix
    if (prefix & 0x40) == 0:
        return ((prefix & 0x3F) << 8) | reader.u8("key index byte 1")
    if (prefix & 0x20) == 0:
        byte_1 = reader.u8("key index byte 1")
        byte_2 = reader.u8("key index byte 2")
        return ((prefix & 0x1F) << 16) | (byte_1 << 8) | byte_2
    if (prefix & 0x10) == 0:
        byte_1 = reader.u8("key index byte 1")
        byte_2 = reader.u8("key index byte 2")
        byte_3 = reader.u8("key index byte 3")
        return ((prefix & 0x0F) << 24) | (byte_1 << 16) | (byte_2 << 8) | byte_3
    return int.from_bytes(reader.read(4, "extended key index"), "big")


def _write_value(value: Any, *, has_key: bool, key_indices: dict[str, int]) -> bytes:
    if has_key:
        raise BPSEError("Dictionary keys must be written by _write_entry")

    if value is None:
        return b"\x10"
    if value is False:
        return b"\x20"
    if value is True:
        return b"\x30"
    if isinstance(value, BPSEReference):
        return _write_extended_uint(0x0A, value.value)
    if isinstance(value, int) and not isinstance(value, bool):
        encoded = _encode_zigzag(value)
        if encoded <= 0x3F:
            return bytes([0x80 | encoded])
        return _write_extended_uint(0x01, encoded)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BPSEError("BPSE JSON float values must be finite")
        return bytes([0x33]) + struct.pack("<d", value)
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= 0x3F:
            return bytes([0x40 | len(encoded)]) + encoded
        return _write_counted_bytes(0x04, encoded)
    if isinstance(value, list):
        count_bytes, width_bits = _write_count(len(value))
        return bytes([width_bits | 0x08]) + count_bytes + b"".join(
            _write_value(item, has_key=False, key_indices=key_indices)
            for item in value
        )
    if isinstance(value, dict):
        if len(value) <= 0x3F:
            tag = bytes([0xC0 | len(value)])
            count_bytes = b""
        else:
            count_bytes, width_bits = _write_count(len(value))
            tag = bytes([width_bits | 0x09])
        return tag + count_bytes + b"".join(
            _write_entry(key, item, key_indices=key_indices)
            for key, item in value.items()
        )

    raise BPSEError(f"Unsupported BPSE value type: {type(value).__name__}")


def _write_entry(key: Any, value: Any, *, key_indices: dict[str, int]) -> bytes:
    return _write_key_index(_resolve_key_index(key, key_indices)) + _write_value(
        value,
        has_key=False,
        key_indices=key_indices,
    )


def _write_key_index(value: int) -> bytes:
    _require_uint(value, 0xFFFFFFFF, "key index")
    if value <= 0x7F:
        return bytes([value])
    if value <= 0x3FFF:
        return bytes([0x80 | (value >> 8), value & 0xFF])
    if value <= 0x1FFFFF:
        return bytes([0xC0 | (value >> 16), (value >> 8) & 0xFF, value & 0xFF])
    if value <= 0x0FFFFFFF:
        return bytes(
            [
                0xE0 | (value >> 24),
                (value >> 16) & 0xFF,
                (value >> 8) & 0xFF,
                value & 0xFF,
            ]
        )
    return b"\xF0" + value.to_bytes(4, "big")


def _write_extended_uint(extended_type: int, value: int) -> bytes:
    width = _smallest_width(value)
    width_bits = {1: 0x00, 2: 0x10, 4: 0x20, 8: 0x30}[width]
    return bytes([width_bits | extended_type]) + _uint_bytes(value, width, "value")


def _write_counted_bytes(extended_type: int, value: bytes) -> bytes:
    count_bytes, width_bits = _write_count(len(value))
    return bytes([width_bits | extended_type]) + count_bytes + value


def _write_count(value: int) -> tuple[bytes, int]:
    width = _smallest_width(value)
    width_bits = {1: 0x00, 2: 0x10, 4: 0x20, 8: 0x30}[width]
    return _uint_bytes(value, width, "count"), width_bits


def _smallest_width(value: int) -> int:
    _require_uint(value, 0xFFFFFFFFFFFFFFFF, "value")
    if value <= 0xFF:
        return 1
    if value <= 0xFFFF:
        return 2
    if value <= 0xFFFFFFFF:
        return 4
    return 8


def _uint_bytes(value: int, width: int, context: str) -> bytes:
    max_value = (1 << (width * 8)) - 1
    _require_uint(value, max_value, context)
    return value.to_bytes(width, "little")


def _require_uint(value: int, max_value: int, context: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > max_value:
        raise BPSEError(f"{context} must be an unsigned integer <= {max_value}")


def _encode_zigzag(value: int) -> int:
    return value << 1 if value >= 0 else ((-value) << 1) - 1


def _decode_zigzag(value: int) -> int:
    return value >> 1 if (value & 1) == 0 else -((value >> 1) + 1)


def _key_name(
    key_index: int,
    key_names: list[str] | tuple[str, ...] | dict[int, str] | None,
) -> str:
    if isinstance(key_names, dict):
        return str(key_names.get(key_index, key_index))
    if key_names is not None and 0 <= key_index < len(key_names):
        return str(key_names[key_index])
    return str(key_index)


def _resolve_key_index(key: Any, key_indices: dict[str, int]) -> int:
    if isinstance(key, int) and not isinstance(key, bool):
        return key
    text = str(key)
    if text in key_indices:
        return key_indices[text]
    if text.isdecimal():
        return int(text)
    raise BPSEError(
        f"Dictionary key {text!r} is not numeric. Pass key_indices to serialize named keys."
    )


def _is_string_table(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _resolve_value_keys(value: Any, string_table: list[str]) -> Any:
    if isinstance(value, list):
        return [_resolve_value_keys(item, string_table) for item in value]
    if isinstance(value, dict):
        resolved: dict[str, Any] = {}
        for key, item in value.items():
            resolved[_resolve_table_key(key, string_table)] = _resolve_value_keys(
                item,
                string_table,
            )
        return resolved
    return value


def _resolve_table_key(key: Any, string_table: list[str]) -> str:
    text = str(key)
    if text.isdecimal():
        index = int(text)
        if 0 <= index < len(string_table):
            return string_table[index]
    return text


def _to_json_value(value: Any) -> JsonValue:
    if isinstance(value, BPSEReference):
        return {BPSE_REFERENCE_MARKER: value.value}
    if isinstance(value, list):
        return [_to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    return value


def _from_json_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_from_json_value(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {BPSE_REFERENCE_MARKER}:
            return BPSEReference(int(value[BPSE_REFERENCE_MARKER]))
        return {str(key): _from_json_value(item) for key, item in value.items()}
    return value


def _looks_like_document_json(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("magic"), str)
        and "root" in value
    )
