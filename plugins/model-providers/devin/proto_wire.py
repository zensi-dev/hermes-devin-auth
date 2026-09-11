"""Minimal protobuf wire codec — just enough for the Devin Cascade API.

The Devin inference surface (``server.codeium.com``) speaks Connect-protocol
protobuf. Hermes ships no ``protobuf`` runtime dependency, so this module
implements the handful of wire operations the plugin needs:

* encoding: varint / fixed32 / fixed64 / length-delimited fields, nested
  messages, repeated strings;
* decoding: a generic field iterator plus typed accessors.

Field numbers mirror the vendored Codeium/Cascade ``exa`` protos the Devin
inference surface uses.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Tuple

WT_VARINT = 0
WT_FIXED64 = 1
WT_LEN = 2
WT_FIXED32 = 5

# (wire_type, value) — value is int for varint, bytes for LEN/fixed.
Field = Tuple[int, object]
Fields = Dict[int, List[Field]]


class ProtoError(ValueError):
    """Malformed wire data."""


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def _varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64  # two's-complement for negative int64
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_no: int, wire_type: int) -> bytes:
    return _varint((field_no << 3) | wire_type)


def f_varint(field_no: int, value: int) -> bytes:
    return _tag(field_no, WT_VARINT) + _varint(int(value))


def f_bool(field_no: int, value: bool) -> bytes:
    return f_varint(field_no, 1 if value else 0)


def f_fixed64(field_no: int, value: float) -> bytes:
    return _tag(field_no, WT_FIXED64) + struct.pack("<d", float(value))


def f_str(field_no: int, value: str) -> bytes:
    data = value.encode("utf-8")
    return _tag(field_no, WT_LEN) + _varint(len(data)) + data


def f_bytes(field_no: int, data: bytes) -> bytes:
    return _tag(field_no, WT_LEN) + _varint(len(data)) + data


def f_msg(field_no: int, message: bytes) -> bytes:
    return f_bytes(field_no, message)


def f_strs(field_no: int, values) -> bytes:
    """Repeated string — never packed; each element gets its own tag."""
    return b"".join(f_str(field_no, v) for v in values)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 70:
            raise ProtoError("varint too long")


def decode(buf: bytes) -> Fields:
    """Decode a message into ``{field_no: [(wire_type, value), ...]}``."""
    fields: Fields = {}
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        field_no, wire = tag >> 3, tag & 0x07
        if field_no == 0:
            raise ProtoError("field number 0")
        if wire == WT_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire == WT_FIXED64:
            if pos + 8 > n:
                raise ProtoError("truncated fixed64")
            value = buf[pos:pos + 8]
            pos += 8
        elif wire == WT_LEN:
            length, pos = _read_varint(buf, pos)
            if pos + length > n:
                raise ProtoError("truncated length-delimited field")
            value = buf[pos:pos + length]
            pos += length
        elif wire == WT_FIXED32:
            if pos + 4 > n:
                raise ProtoError("truncated fixed32")
            value = buf[pos:pos + 4]
            pos += 4
        elif wire in (3, 4):  # deprecated groups — skip
            continue
        else:
            raise ProtoError(f"unknown wire type {wire}")
        fields.setdefault(field_no, []).append((wire, value))
    return fields


def _first(fields: Fields, field_no: int) -> Field | None:
    values = fields.get(field_no)
    return values[0] if values else None


def get_str(fields: Fields, field_no: int, default: str = "") -> str:
    f = _first(fields, field_no)
    if f is None or f[0] != WT_LEN:
        return default
    return f[1].decode("utf-8", errors="replace")


def get_strs(fields: Fields, field_no: int) -> List[str]:
    return [v.decode("utf-8", errors="replace") for w, v in fields.get(field_no, []) if w == WT_LEN]


def get_int(fields: Fields, field_no: int, default: int = 0) -> int:
    f = _first(fields, field_no)
    if f is None or f[0] != WT_VARINT:
        return default
    return int(f[1])


def get_bool(fields: Fields, field_no: int, default: bool = False) -> bool:
    f = _first(fields, field_no)
    if f is None or f[0] != WT_VARINT:
        return default
    return bool(f[1])


def get_msg(fields: Fields, field_no: int) -> Fields | None:
    f = _first(fields, field_no)
    if f is None or f[0] != WT_LEN:
        return None
    return decode(f[1])


def get_msgs(fields: Fields, field_no: int) -> List[Fields]:
    return [decode(v) for w, v in fields.get(field_no, []) if w == WT_LEN]
