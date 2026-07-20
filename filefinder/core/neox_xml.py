"""Minimal NeoX binary XML reader used for file tracking."""

from __future__ import annotations

from collections import deque
from io import BytesIO
import struct
import xml.etree.ElementTree as ET


NEOX_BINARY_MAGIC = b"\xC1\x59\x41\x0D"


def neox_bytes_to_text(data: bytes) -> str:
    """Return XML text from a NeoX XML or C1 59 41 0D binary XML payload."""
    if data.startswith(NEOX_BINARY_MAGIC):
        roots = _binary_to_roots(data)
        return _roots_to_pretty_text(roots)
    text = data.decode("utf-8-sig")
    try:
        return _roots_to_pretty_text(_parse_xml_roots(text))
    except ET.ParseError:
        return text


def _roots_to_pretty_text(roots: list[ET.Element]) -> str:
    output: list[str] = []
    for root in roots:
        ET.indent(root, space="    ")
        output.append(ET.tostring(root, encoding="unicode"))
    return "\n".join(output)


def _parse_xml_roots(text: str) -> list[ET.Element]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return [ET.fromstring(stripped)]
    except ET.ParseError:
        wrapped = f"<FileFinderRoot>{stripped}</FileFinderRoot>"
        wrapper = ET.fromstring(wrapped)
        return list(wrapper)


def _read_leb128(stream: BytesIO) -> int:
    value = 0
    shift = 0
    while True:
        raw_byte = stream.read(1)
        if not raw_byte:
            raise EOFError("Unexpected end of NeoX binary XML data")
        byte = raw_byte[0]
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value
        shift += 7


def _read_null_string(stream: BytesIO) -> str:
    collected = bytearray()
    while True:
        raw_byte = stream.read(1)
        if not raw_byte:
            raise EOFError("Unexpected end of NeoX string data")
        if raw_byte == b"\x00":
            return collected.decode("utf-8")
        collected += raw_byte


def _read_definitions(stream: BytesIO, amount: int) -> list[str]:
    return [_read_null_string(stream) for _ in range(amount)]


def _read_attribute_value(stream: BytesIO, data_type: bytes) -> str:
    if data_type == b"\x01":
        return _read_null_string(stream)
    if data_type == b"\x02":
        return str(struct.unpack("<I", stream.read(4))[0])
    if data_type == b"\x05":
        return str(struct.unpack("<i", stream.read(4))[0])
    if data_type == b"\x08":
        return str(struct.unpack("<Q", stream.read(8))[0])
    if data_type == b"\x06":
        matrix_size = struct.unpack("<I", stream.read(4))[0]
        values = [f"{struct.unpack('<f', stream.read(4))[0]:.4f}" for _ in range(matrix_size)]
        return ",".join(values)
    raise ValueError(f"Unsupported NeoX binary XML attribute type: {data_type.hex().upper()}")


def _binary_to_roots(data: bytes) -> list[ET.Element]:
    stream = BytesIO(data)
    if stream.read(4) != NEOX_BINARY_MAGIC:
        raise ValueError("Invalid NeoX binary XML magic")

    stream.read(8)
    element_definitions = _read_definitions(stream, _read_leb128(stream))
    attribute_definitions = _read_definitions(stream, _read_leb128(stream))
    stream.read(8)

    tag_amount = _read_leb128(stream)
    element_tags: list[tuple[str, int]] = []
    for _ in range(tag_amount):
        element_id = _read_leb128(stream)
        child_count = _read_leb128(stream)
        element_tags.append((element_definitions[element_id], child_count))

    attribute_map: list[dict[str, str]] = []
    for _ in range(tag_amount):
        attribute_amount_raw = stream.read(1)
        if not attribute_amount_raw:
            raise EOFError("Unexpected end of NeoX attribute map")
        attributes: dict[str, str] = {}
        for _ in range(attribute_amount_raw[0]):
            attribute_id = stream.read(1)[0]
            data_type = stream.read(1)
            attributes[attribute_definitions[attribute_id]] = _read_attribute_value(stream, data_type)
        if stream.read(2) != b"\x01\x00":
            raise ValueError("Invalid NeoX attribute terminator")
        attribute_map.append(attributes)

    return _wrap_tags(element_tags, attribute_map)


def _wrap_tags(
    element_tags: list[tuple[str, int]],
    attribute_map: list[dict[str, str]],
) -> list[ET.Element]:
    roots: list[ET.Element] = []
    queue: deque[tuple[ET.Element, int]] = deque()

    for index, (tag, child_count) in enumerate(element_tags):
        element = ET.Element(tag, attribute_map[index])
        if not queue:
            roots.append(element)
        else:
            while queue and queue[0][1] == 0:
                queue.popleft()
            parent, remaining = queue[0]
            parent.append(element)
            queue[0] = (parent, remaining - 1)

        if child_count:
            queue.append((element, child_count))

    return roots
