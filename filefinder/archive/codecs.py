"""Payload decoding helpers for IDX/WPK entries."""

from __future__ import annotations

import logging
import struct
import zlib
from enum import IntFlag, auto

LOG = logging.getLogger(__name__)

try:
    from cryptography.hazmat.backends import default_backend as aes_default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    HAS_AES = True
except Exception:
    Cipher = algorithms = modes = aes_default_backend = None
    HAS_AES = False


class EntryDataFlags(IntFlag):
    NONE = 0
    TEXT = auto()
    NXS3_PACKED = auto()
    ROTOR_PACKED = auto()
    ERROR = auto()


def _u16(data: bytes) -> int:
    return int.from_bytes(data, "little")


def derive_key(length: int, tag_byte: int) -> bytes:
    value = (tag_byte + (length & 0xFFFFFFFF)) & 0xFF
    first = (
        0x7C2E6B6A00000000
        | (((length & 0xFFFFFFFF) << 8) & 0xFFFF0000)
        | (value << 8)
        | (length % 0xFD)
    )
    second = (
        0x5C74656E00003630
        | (((value ^ 0x33) << 16) & 0xFFFFFFFF00FFFFFF)
        | ((value | 0x2E) << 24)
    )
    return struct.pack("<QQ", first & 0xFFFFFFFFFFFFFFFF, second & 0xFFFFFFFFFFFFFFFF)


def aes_decrypt_prefix(buffer: bytearray, length: int, key16: bytes) -> int:
    if length <= 0 or not HAS_AES:
        return 0

    done = (length // 16) * 16
    if done <= 0:
        return 0

    cipher = Cipher(algorithms.AES(key16), modes.ECB(), backend=aes_default_backend())
    decryptor = cipher.decryptor()
    buffer[:done] = decryptor.update(bytes(buffer[:done])) + decryptor.finalize()
    return done


def xor_offset(buffer: bytearray, offset: int, wanted: int, seed: int) -> None:
    if wanted <= 0:
        return
    mirror_len = min(offset, wanted)
    for index in range(mirror_len):
        buffer[offset + index] ^= ((seed + index) + buffer[index]) & 0xFF
    for index in range(wanted - mirror_len):
        buffer[offset + mirror_len + index] ^= (seed + mirror_len + index) & 0xFF


def xor_linear(buffer: bytearray, wanted: int, seed: int) -> None:
    for index in range(wanted):
        buffer[index] ^= (seed + index) & 0xFF


def header_decode(buffer: bytearray) -> None:
    count = min(64, len(buffer))
    left, right = 0, count - 1
    while left < right:
        left_value = buffer[left] ^ 0x5A
        right_value = buffer[right] ^ 0x5A
        buffer[left], buffer[right] = right_value, left_value
        left += 1
        right -= 1
    if left == right:
        buffer[left] ^= 0x5A


def decode_payload_stage1(
    payload: bytes,
    *,
    skip_header_decode: bool = False,
) -> tuple[bytes, int] | None:
    if len(payload) < 8:
        return None

    tag = _u16(payload[0:2])
    prefix_power = payload[2]
    tag_byte = payload[3]
    body = bytearray(payload[8:])
    body_len = len(body)
    prefix_len = min(body_len, 128 << (prefix_power - 1)) if body_len > 0 and prefix_power else 0
    seed = (tag_byte + body_len) & 0xFFFFFFFF

    if tag in (0x4341, 0x4350):
        done = aes_decrypt_prefix(body, prefix_len, derive_key(body_len, tag_byte))
        remaining = max(0, prefix_len - done)
        if remaining:
            xor_offset(body, done, remaining, seed)
    elif tag == 0x4358:
        xor_linear(body, prefix_len, seed)
    else:
        return None

    if not skip_header_decode:
        header_decode(body)
    return bytes(body), tag


def try_decode_payload_stage1(
    payload: bytes,
    *,
    context: str = "",
    skip_header_decode: bool = False,
) -> tuple[bytes, bool, int | None]:
    try:
        result = decode_payload_stage1(payload, skip_header_decode=skip_header_decode)
    except Exception as exc:
        LOG.debug("WPD1 stage1 failed for %s: %s", context or "-", exc)
        return payload, False, None

    if result is None:
        return payload, False, None

    decoded, tag = result
    return decoded, True, tag


class Rotor:
    def __init__(self, key: str, n_rotors: int = 6):
        self.n_rotors = n_rotors
        self.key = key
        self.rotors = None
        self.positions = [None, None]

    def decrypt(self, buffer: bytes) -> bytes:
        self.positions[1] = None
        return self.cryptmore(buffer, 1)

    def cryptmore(self, buffer: bytes, do_decrypt: int) -> bytes:
        size, rotor_count, rotors, positions = self.get_rotors(do_decrypt)
        output = b""
        for value in buffer:
            if do_decrypt:
                for index in range(rotor_count - 1, -1, -1):
                    value = positions[index] ^ rotors[index][value]
            else:
                for index in range(rotor_count):
                    value = rotors[index][value ^ positions[index]]
            output += value.to_bytes(1, "big")

            next_position = 0
            for index in range(rotor_count):
                next_position = ((positions[index] + (next_position >= size)) & 0xFF) + rotors[index][size]
                positions[index] = next_position % size
        return output

    def get_rotors(self, do_decrypt: int):
        rotor_count = self.n_rotors
        rotors = self.rotors
        positions = self.positions[do_decrypt]
        if positions is None:
            if rotors:
                positions = list(rotors[3])
            else:
                size = 256
                id_rotor = list(range(size + 1))
                rand = self.random_func(self.key)
                enc_rotors = []
                dec_rotors = []
                positions = []
                for _ in range(rotor_count):
                    index = size
                    positions.append(rand(index))
                    enc = id_rotor[:]
                    dec = id_rotor[:]
                    dec[index] = enc[index] = 1 + 2 * rand(index / 2)
                    while index > 1:
                        random_index = rand(index)
                        index -= 1
                        value = enc[random_index]
                        enc[random_index] = enc[index]
                        enc[index] = value
                        dec[value] = index
                    dec[enc[0]] = 0
                    enc_rotors.append(tuple(enc))
                    dec_rotors.append(tuple(dec))
                self.rotors = rotors = (tuple(enc_rotors), tuple(dec_rotors), size, tuple(positions))
            self.positions[do_decrypt] = positions
        return rotors[2], rotor_count, rotors[do_decrypt], positions

    @staticmethod
    def random_func(key: str):
        mask = 0xFFFF
        x = 995
        y = 576
        z = 767
        for char in map(ord, key):
            x = (((x << 3 | x >> 13) + char) & mask)
            y = (((y << 3 | y >> 13) ^ char) & mask)
            z = (((z << 3 | z >> 13) - char) & mask)

        max_position = mask >> 1
        mask += 1
        if x > max_position:
            x -= mask
        if y > max_position:
            y -= mask
        if z > max_position:
            z -= mask

        y |= 1
        x = 171 * (int(x) % 177) - 2 * (int(x) // 177)
        y = 172 * (int(y) % 176) - 35 * (int(y) // 176)
        z = 170 * (int(z) % 178) - 63 * (int(z) // 178)
        if x < 0:
            x += 30269
        if y < 0:
            y += 30307
        if z < 0:
            z += 30323

        def rand(n, seed=[(x, y, z)]):
            x0, y0, z0 = seed[0]
            seed[0] = ((171 * x0) % 30269, (172 * y0) % 30307, (170 * z0) % 30323)
            return int(int((x0 / 30269 + y0 / 30307 + z0 / 30323) * n) % n)

        return rand


def init_rotor() -> Rotor:
    name = "j2h56ogodh3se"
    token = "=dziaq."
    footer = '|os=5v7!"-234'
    key = name * 4 + (token + name + footer) * 5 + "!" + "#" + token * 7 + footer * 2 + "*" + "&" + "'"
    return Rotor(key)


def strip_none_wrapper(data: bytes) -> bytes:
    return data[4:] if data[:4] == b"NONE" else data


def check_lz4_like(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == b"\x27\xe3\x00\x01"


def unpack_lz4_like(data: bytes) -> bytes:
    if not data:
        return b""

    in_ptr = 0
    out = bytearray()
    data_len = len(data)
    while in_ptr < data_len:
        token = data[in_ptr]
        in_ptr += 1
        literal_len = token >> 4
        match_len = token & 0x0F

        if literal_len == 15:
            while in_ptr < data_len:
                value = data[in_ptr]
                in_ptr += 1
                literal_len += value
                if value != 0xFF:
                    break

        if in_ptr + literal_len > data_len:
            break
        out.extend(data[in_ptr : in_ptr + literal_len])
        in_ptr += literal_len
        if in_ptr >= data_len or in_ptr + 2 > data_len:
            break

        offset = struct.unpack("<H", data[in_ptr : in_ptr + 2])[0]
        in_ptr += 2
        if match_len == 15:
            while in_ptr < data_len:
                value = data[in_ptr]
                in_ptr += 1
                match_len += value
                if value != 0xFF:
                    break

        start = len(out) - offset
        if start < 0:
            break
        for index in range(match_len + 4):
            if start + index >= len(out):
                break
            out.append(out[start + index])
    return bytes(out)


def check_rotor(data: bytes) -> bool:
    return data[:2] in (bytes([0x1D, 0x04]), bytes([0x15, 0x23]))


def unpack_rotor(data: bytes) -> bytes:
    values = list(zlib.decompress(init_rotor().decrypt(data)))
    values = list(map(lambda value: value ^ 154, values[0:128])) + values[128:]
    values.reverse()
    return bytes(values)


def check_nxs3(data: bytes) -> bool:
    return data[:8] in (b"NXS3\x03\x00\x00\x01", b"\x4e\x58\x5a\x00\x47\x38\x36\x00")


def rsa_public_decrypt(signature: bytes, key) -> bytes:
    public_numbers = key.public_numbers()
    byte_count = (public_numbers.n.bit_length() + 7) // 8
    if len(signature) != byte_count:
        raise ValueError("Signature length does not match key size")

    decrypted = pow(
        int.from_bytes(signature, byteorder="big"),
        public_numbers.e,
        public_numbers.n,
    ).to_bytes(byte_count, byteorder="big")
    if decrypted[0] != 0x00 or decrypted[1] != 0x01:
        raise ValueError("Incorrect RSA padding")
    padding_end = decrypted.index(0x00, 2)
    return decrypted[padding_end + 1 :]


def _load_rsa_public_key(pem_key: bytes):
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
    except Exception as exc:
        raise RuntimeError("NXS unpack requires cryptography") from exc
    return serialization.load_pem_public_key(pem_key, backend=default_backend())


def _nxs3_logic(data: bytes, key, size: int) -> bytes:
    wrapped_key = rsa_public_decrypt(data[20 : 20 + size], key)[:4]
    if len(wrapped_key) != 4:
        raise ValueError("Encrypted key decrypt failed")

    ephemeral_key = int.from_bytes(wrapped_key, "little")
    decrypted = bytearray()
    payload = data[20 + size :]
    for index, value in enumerate(payload):
        decrypted.append(value ^ ((ephemeral_key >> (index % 4 * 8)) & 0xFF))
        if index % 4 == 3:
            rotated = (ephemeral_key >> 19) | ((ephemeral_key << 13) & 0xFFFFFFFF)
            ephemeral_key = (rotated + ((rotated << 2) & 0xFFFFFFFF) + 0xE6546B64) & 0xFFFFFFFF
    return bytes(decrypted)


def _unpack_nxs3_old(data: bytes) -> bytes:
    pem_key = b"""-----BEGIN RSA PUBLIC KEY-----
MIGJAoGBAOZAaZe2qB7dpT9Y8WfZIdDv+ooS1HsFEDW2hFnnvcuFJ4vIuPgKhISm
pY4/jT3aipwPNVTjM6yHbzOLhrnGJh7Ec3CQG/FZu6VKoCqVEtCeh15hjcu6QYtn
YWIEf8qgkylqsOQ3IIn76udV6m0AWC2jDlmLeRcR04w9NNw7+9t9AgMBAAE=
-----END RSA PUBLIC KEY-----"""
    return _nxs3_logic(data, _load_rsa_public_key(pem_key), 128)


def _unpack_nxs_new(data: bytes) -> bytes:
    pem_key = b"""-----BEGIN RSA PUBLIC KEY-----
MIICCgKCAgEAu5/HBdUwY37hJbm3ri9h/fHJqsx6PeLTEqP2tIYoV3+qn0lI4Kht
wi03S2wf6CrwWXuf8Dp4L/MRsFi/Cxqe53m6Dhx8Zy9nzStaBUzp0DeL/M+HWI+r
fDUPybKfJx9qlTNxUyvIQZkSh83YdkhVC4pqiOt0nGCS44Xs88DEkYOjRydLa4uK
JQIZAuUSsC5Cu9FjBzGHW3Pc9ene9HJai+8ipvi8bhLc1hnvlER7GtzQce/Ubjq2
D79KXLCjZKYr0L+9h7hfOQk+R2VqVthRvuf2ql9H13Wbnukm6ijg8+mamB6esNTo
OPdjQkuMj5wUEfPqRK3GZibW92QilOvFt9cx0JBjjs3k8ax7u9iOnsVEqUqgX9bE
FoZiwUfV1wJAcfEzJqJ4/wMe8FIV35Pg9UE/tQ4M9YX+PDUTnaWXksK8kDqa96NG
d9xqy+MntsUcKf7UsEExtkm6GDxtpIokUYplUAMPQDo/04eBOP6J5YdjOv2Dxjd5
OM832KIu1uYdO81xRGmyiSsavtzkQJbePWVFq1iW/1+nmaodzgi/esbLFM5T6xan
iOvQK1rRaJgE2NdU0EOAOhDAJu+1JfiB60nJw20gSM6Wl3s9N+UmXrR+xJxxcgnK
P0VB60qOgnlYmNwld5muJazI9P7sbtFRuEVLoN5Y+P9PCIXQ/RrZVLMCAwEAAQ==
-----END RSA PUBLIC KEY-----"""
    decrypted = _nxs3_logic(data, _load_rsa_public_key(pem_key), 512)
    if decrypted[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard
        except Exception as exc:
            raise RuntimeError("New NXS zstd payload requires zstandard") from exc
        return zstandard.ZstdDecompressor().decompress(decrypted)
    return decrypted


def unpack_nxs3(data: bytes) -> bytes:
    if data[:8] == b"NXS3\x03\x00\x00\x01":
        return _unpack_nxs3_old(data)
    if data[:8] == b"\x4e\x58\x5a\x00\x47\x38\x36\x00":
        return _unpack_nxs_new(data)
    return data


def maybe_unpack_dtsz(data: bytes, *, context: str) -> tuple[bytes, bool]:
    if len(data) < 8 or data[:4] != b"DTSZ" or data[4:8] != b"\x28\xB5\x2F\xFD":
        return data, False
    try:
        import zstandard
    except Exception as exc:
        LOG.debug("DTSZ support unavailable for %s: %s", context, exc)
        return data, False
    try:
        return zstandard.ZstdDecompressor().decompress(data[4:]), True
    except Exception as exc:
        LOG.warning("DTSZ decompress failed for %s: %s", context, exc)
        return data, False


def maybe_strip_enon_header(data: bytes) -> tuple[bytes, bool]:
    if len(data) < 4 or data[:4] != b"ENON":
        return data, False
    return data[4:], True


def deobfuscate_cobl_probe_region(data: bytes) -> tuple[bytes, int]:
    if not data:
        return data, 0
    probe_len = min(64, len(data))
    if probe_len <= 3:
        return data, 0
    patched = bytearray(data)
    patched[:probe_len] = bytes((value ^ 0x5A) for value in data[:probe_len][::-1])
    return bytes(patched), probe_len


def decode_cobl_block(data: bytes, *, context: str) -> bytes:
    patched, probe_len = deobfuscate_cobl_probe_region(data)
    if probe_len < 4 or len(patched) < 4:
        return data
    tag = struct.unpack_from("<I", patched, 0)[0]
    payload = patched[4:]
    if tag == 0x4E4F4E45:
        return payload
    if tag == 0x5A4C4942:
        return zlib.decompress(payload)
    if tag == 0x5A535444:
        try:
            import zstandard
        except Exception as exc:
            raise RuntimeError(f"COBL block requires zstandard for {context}") from exc
        return zstandard.ZstdDecompressor().decompress(payload)
    if tag == 0x4C5A3446:
        try:
            import lz4.frame as lz4f
        except Exception as exc:
            raise RuntimeError(f"COBL block requires lz4.frame for {context}") from exc
        return lz4f.decompress(payload)
    if tag == 0x4F4F444C:
        raise RuntimeError(f"COBL block uses unsupported Oodle codec for {context}")
    return data


def decode_cobl_concat(data: bytes, *, context: str) -> bytes:
    magic, _field04, _field08, block_count = struct.unpack_from("<4I", data, 0)
    if magic != 0x434F424C:
        raise ValueError(f"Bad COBL magic 0x{magic:08X}")

    data_base = 16 + block_count * 8
    if len(data) < data_base:
        raise ValueError(f"COBL block table is truncated: need >= {data_base}, got {len(data)}")

    relative_offset = 0
    output = bytearray()
    for block_index in range(block_count):
        size, extra, _unknown = struct.unpack_from("<IHH", data, 16 + block_index * 8)
        start = data_base + relative_offset
        end = start + size
        if end > len(data):
            raise ValueError(
                f"COBL block {block_index} is out of range: start={start}, end={end}, size={len(data)}"
            )
        output.extend(decode_cobl_block(data[start:end], context=f"{context} block={block_index}"))
        relative_offset += size + extra
    return bytes(output)


def maybe_unpack_cobl(data: bytes, *, context: str) -> tuple[bytes, bool]:
    if len(data) < 16 or data[:4] not in (b"LBOC", b"COBL"):
        return data, False
    try:
        unpacked = decode_cobl_concat(data, context=context)
        return (unpacked, True) if unpacked else (data, False)
    except Exception as exc:
        LOG.warning("COBL decode failed for %s: %s", context, exc)
        return data, False


def is_binary(data: bytes) -> bool:
    if b"\x00" in data[:4000]:
        return True
    try:
        data[:2048].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return True
    return False


def score_slot_stage1_candidate(data: bytes) -> tuple[int, str]:
    if not data:
        return -1000, "empty"

    known_heads = (
        b"ENON",
        b"NXS3",
        b"LBOC",
        b"RIFF",
        b"OggS",
        b"FSB5",
        b"BKHD",
        b"DDS ",
        b"PVR",
        b"RGIS",
        b"VANT",
        b"NTRK",
        b"PK\x03\x04",
        b"CompBlks",
        b"SKELETON",
        b"NEOXBIN1",
        b"NEOXMESH",
        b"RAWANIMA",
    )
    score = 0
    reasons: list[str] = []
    if any(data.startswith(head) for head in known_heads):
        score += 120
        reasons.append("known_magic")

    head = data[:64]
    if head:
        z5a = head.count(0x5A)
        if z5a >= 16:
            score -= min(80, z5a * 2)
            reasons.append(f"z5a={z5a}")
        unique = len(set(head))
        if unique <= 8:
            score -= 40
            reasons.append(f"uniq={unique}")
        elif unique >= 24:
            score += 15
            reasons.append(f"uniq={unique}")

    if not is_binary(data):
        score += 30
        reasons.append("text")
    elif data[:4].isalpha():
        score += 20
        reasons.append("alpha_head")
    return score, ",".join(reasons) if reasons else "none"


def decode_slot_payload_auto(payload: bytes, *, context: str) -> tuple[bytes, bool, int | None, bool]:
    with_header, with_decoded, with_tag = try_decode_payload_stage1(
        payload,
        context=f"{context} slot_auto with_header",
        skip_header_decode=False,
    )
    no_header, no_decoded, no_tag = try_decode_payload_stage1(
        payload,
        context=f"{context} slot_auto no_header",
        skip_header_decode=True,
    )
    if with_decoded and no_decoded:
        with_score, _with_reason = score_slot_stage1_candidate(with_header)
        no_score, _no_reason = score_slot_stage1_candidate(no_header)
        choose_no_header = no_score > with_score
        return (
            no_header if choose_no_header else with_header,
            True,
            no_tag if choose_no_header else with_tag,
            choose_no_header,
        )
    if no_decoded:
        return no_header, True, no_tag, True
    if with_decoded:
        return with_header, True, with_tag, False
    return payload, False, None, False


def unwrap_nested_payloads(data: bytes, *, context: str) -> tuple[bytes, list[str], EntryDataFlags]:
    layers: list[str] = []
    flags = EntryDataFlags.NONE
    seen: set[tuple[int, bytes]] = set()

    for _ in range(32):
        signature = (len(data), data[:16])
        if signature in seen:
            break
        seen.add(signature)

        stripped = strip_none_wrapper(data)
        if stripped != data:
            data = stripped
            layers.append("NONE")
            continue

        stripped, did_strip = maybe_strip_enon_header(data)
        if did_strip:
            data = stripped
            layers.append("ENON")
            continue

        unpacked, did_unpack = maybe_unpack_dtsz(data, context=context)
        if did_unpack:
            data = unpacked
            layers.append("DTSZ")
            continue

        unpacked, did_unpack = maybe_unpack_cobl(data, context=context)
        if did_unpack:
            data = unpacked
            layers.append("COBL")
            continue

        if check_lz4_like(data):
            try:
                unpacked = unpack_lz4_like(data)
            except Exception as exc:
                LOG.warning("LZ4-like unpack failed for %s: %s", context, exc)
                break
            if unpacked and unpacked != data:
                data = unpacked
                layers.append("LZ4_LIKE")
                continue

        if check_rotor(data):
            try:
                data = unpack_rotor(data)
            except Exception as exc:
                LOG.warning("ROTOR unpack failed for %s: %s", context, exc)
                break
            flags |= EntryDataFlags.ROTOR_PACKED
            layers.append("ROTOR")
            continue

        if check_nxs3(data):
            try:
                data = unpack_nxs3(data)
            except Exception as exc:
                LOG.warning("NXS unpack failed for %s: %s", context, exc)
                break
            flags |= EntryDataFlags.NXS3_PACKED
            layers.append("NXS3")
            continue

        break
    else:
        LOG.warning("Nested payload unwrap hit layer limit for %s", context)

    if not is_binary(data):
        flags |= EntryDataFlags.TEXT
    return data, layers, flags

