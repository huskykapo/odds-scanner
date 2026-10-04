"""Minimal schema-less protobuf reader (no dependency, no .proto file).

Protobuf messages are a flat list of ``(field number, wire type, value)`` records. Without a
schema a length-delimited value may be a string, bytes or a nested message - the caller decides
by asking for :meth:`Message.text` or :meth:`Message.message`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

VARINT, FIXED64, LENGTH, FIXED32 = 0, 1, 2, 5


class DecodeError(ValueError):
    pass


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise DecodeError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


@dataclass(frozen=True)
class Field:
    number: int
    wire: int
    raw: int | bytes  # int for varints, bytes otherwise


def iter_fields(buf: bytes) -> Iterator[Field]:
    """Yield the records of one message. Raises :class:`DecodeError` on malformed input."""
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise DecodeError("field number 0")
        if wire == VARINT:
            value, pos = _varint(buf, pos)
            yield Field(number, wire, value)
        elif wire == FIXED64:
            if pos + 8 > len(buf):
                raise DecodeError("truncated fixed64")
            yield Field(number, wire, buf[pos : pos + 8])
            pos += 8
        elif wire == LENGTH:
            size, pos = _varint(buf, pos)
            if pos + size > len(buf):
                raise DecodeError("truncated length-delimited field")
            yield Field(number, wire, buf[pos : pos + size])
            pos += size
        elif wire == FIXED32:
            if pos + 4 > len(buf):
                raise DecodeError("truncated fixed32")
            yield Field(number, wire, buf[pos : pos + 4])
            pos += 4
        else:
            raise DecodeError(f"unsupported wire type {wire}")


class Message:
    """A decoded message with typed accessors. Unknown / mistyped fields read as None."""

    __slots__ = ("fields",)

    def __init__(self, buf: bytes, *, lenient: bool = False) -> None:
        """Decode ``buf``. ``lenient`` keeps the fields read before a truncation/garbage point
        (for responses cut short) instead of raising :class:`DecodeError`."""
        self.fields: dict[int, list[Field]] = {}
        try:
            for f in iter_fields(buf):
                self.fields.setdefault(f.number, []).append(f)
        except DecodeError:
            if not lenient:
                raise

    @classmethod
    def try_parse(cls, buf: bytes) -> "Message | None":
        if not buf:
            return None
        try:
            return cls(buf)
        except DecodeError:
            return None

    def _first(self, number: int, wire: int) -> Field | None:
        return next((f for f in self.fields.get(number, ()) if f.wire == wire), None)

    def int(self, number: int) -> int | None:
        f = self._first(number, VARINT)
        return f.raw if f else None  # type: ignore[return-value]

    def text(self, number: int) -> str | None:
        f = self._first(number, LENGTH)
        if f is None:
            return None
        try:
            return f.raw.decode("utf-8")  # type: ignore[union-attr]
        except UnicodeDecodeError:
            return None

    def float32(self, number: int) -> float | None:
        f = self._first(number, FIXED32)
        return struct.unpack("<f", f.raw)[0] if f else None  # type: ignore[arg-type]

    def double(self, number: int) -> float | None:
        f = self._first(number, FIXED64)
        return struct.unpack("<d", f.raw)[0] if f else None  # type: ignore[arg-type]

    def message(self, number: int) -> "Message | None":
        f = self._first(number, LENGTH)
        return Message.try_parse(f.raw) if f else None  # type: ignore[arg-type]

    def messages(self, number: int) -> list["Message"]:
        out = []
        for f in self.fields.get(number, ()):
            if f.wire == LENGTH:
                m = Message.try_parse(f.raw)  # type: ignore[arg-type]
                if m is not None:
                    out.append(m)
        return out

    def submessages(self) -> Iterator["Message"]:
        """Every length-delimited field that parses as a message (strings usually do not)."""
        for fields in self.fields.values():
            for f in fields:
                if f.wire == LENGTH:
                    m = Message.try_parse(f.raw)  # type: ignore[arg-type]
                    if m is not None:
                        yield m
