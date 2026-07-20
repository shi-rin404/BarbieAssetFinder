"""Small FlatBuffers reader for THY lookup tables."""

from pathlib import Path


class ParseError(RuntimeError):
    """Raised when an asset index or lookup file cannot be parsed."""


class PathNotFoundError(LookupError):
    """Raised when a normalized asset path is not present in a THY table."""


class BinaryReader:
    def __init__(self, data: bytes, source: Path):
        self.data = data
        self.source = source

    def require(self, offset: int, size: int, context: str) -> None:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise ParseError(
                f"{context}: read outside file bounds; offset=0x{offset:X}, "
                f"size=0x{size:X}, file_size=0x{len(self.data):X}"
            )

    def u8(self, offset: int, context: str = "u8") -> int:
        self.require(offset, 1, context)
        return self.data[offset]

    def u16(self, offset: int, context: str = "u16") -> int:
        self.require(offset, 2, context)
        return int.from_bytes(self.data[offset : offset + 2], "little")

    def u32(self, offset: int, context: str = "u32") -> int:
        self.require(offset, 4, context)
        return int.from_bytes(self.data[offset : offset + 4], "little")

    def i32(self, offset: int, context: str = "i32") -> int:
        self.require(offset, 4, context)
        return int.from_bytes(self.data[offset : offset + 4], "little", signed=True)

    def read(self, offset: int, size: int, context: str = "bytes") -> bytes:
        self.require(offset, size, context)
        return self.data[offset : offset + size]


def fb_field_offset(reader: BinaryReader, table: int, vtable_entry_offset: int) -> int:
    vtable_distance = reader.i32(table, "FlatBuffer vtable distance")
    vtable = table - vtable_distance
    vtable_size = reader.u16(vtable, "FlatBuffer vtable size")
    if vtable_entry_offset >= vtable_size:
        return 0
    return reader.u16(vtable + vtable_entry_offset, "FlatBuffer field offset")


def fb_pointer(
    reader: BinaryReader,
    table: int,
    vtable_entry_offset: int,
) -> int | None:
    field_offset = fb_field_offset(reader, table, vtable_entry_offset)
    if field_offset == 0:
        return None

    field_address = table + field_offset
    relative = reader.u32(field_address, "FlatBuffer uoffset")
    if relative == 0:
        return None

    target = field_address + relative
    reader.require(target, 4, "FlatBuffer pointer target")
    return target


def fb_required_pointer(
    reader: BinaryReader,
    table: int,
    vtable_entry_offset: int,
    name: str,
) -> int:
    result = fb_pointer(reader, table, vtable_entry_offset)
    if result is None:
        raise ParseError(f"Required FlatBuffer field is missing: {name}")
    return result


def read_u32_vector(reader: BinaryReader, vector: int, name: str) -> list[int]:
    count = reader.u32(vector, f"{name} count")
    byte_size = count * 4
    reader.require(vector + 4, byte_size, f"{name} data")
    return [reader.u32(vector + 4 + index * 4, f"{name}[{index}]") for index in range(count)]

