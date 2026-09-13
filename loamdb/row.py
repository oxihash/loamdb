"""Row encoding: turns a Python tuple of column values into the bytes
stored as a B+tree value, and back. Every column value (including the
primary key, which is redundant with the B+tree key it's stored under)
is written out, in schema order -- one extra copy of the primary key per
row in exchange for a decoder that never has to reconstruct a value from
its own encoded key bytes. On a toy database that trade is worth making.
"""
from __future__ import annotations

import struct

NULL, INTEGER, TEXT = 0, 1, 2


class Schema:
    def __init__(self, name: str, columns: list):
        self.name = name
        self.columns = columns  # list of (col_name: str, col_type: str, is_primary_key: bool)

    def primary_key_index(self) -> int:
        for i, (_, _, is_pk) in enumerate(self.columns):
            if is_pk:
                return i
        raise ValueError(f"table '{self.name}' has no PRIMARY KEY column")

    def column_index(self, name: str) -> int:
        for i, (col_name, _, _) in enumerate(self.columns):
            if col_name == name:
                return i
        raise ValueError(f"table '{self.name}' has no column '{name}'")

    def to_bytes(self) -> bytes:
        out = bytearray()
        out += struct.pack("<H", len(self.name))
        out += self.name.encode("utf-8")
        out += struct.pack("<H", len(self.columns))
        for col_name, col_type, is_pk in self.columns:
            out += struct.pack("<HBB", len(col_name), 1 if col_type == "INTEGER" else 2, 1 if is_pk else 0)
            out += col_name.encode("utf-8")
        return bytes(out)

    @staticmethod
    def from_bytes(data: bytes) -> "Schema":
        offset = 0
        (nlen,) = struct.unpack_from("<H", data, offset); offset += 2
        name = data[offset:offset + nlen].decode("utf-8"); offset += nlen
        (ncols,) = struct.unpack_from("<H", data, offset); offset += 2
        columns = []
        for _ in range(ncols):
            clen, type_tag, is_pk = struct.unpack_from("<HBB", data, offset); offset += 4
            col_name = data[offset:offset + clen].decode("utf-8"); offset += clen
            columns.append((col_name, "INTEGER" if type_tag == 1 else "TEXT", bool(is_pk)))
        return Schema(name, columns)


def encode_row(schema: Schema, values: list) -> bytes:
    out = bytearray()
    for (col_name, col_type, _), value in zip(schema.columns, values):
        if value is None:
            out += struct.pack("<B", NULL)
        elif col_type == "INTEGER":
            out += struct.pack("<Bq", INTEGER, value)
        else:
            encoded = str(value).encode("utf-8")
            out += struct.pack("<BI", TEXT, len(encoded))
            out += encoded
    return bytes(out)


def decode_row(schema: Schema, data: bytes) -> list:
    values = []
    offset = 0
    for _ in schema.columns:
        (tag,) = struct.unpack_from("<B", data, offset); offset += 1
        if tag == NULL:
            values.append(None)
        elif tag == INTEGER:
            (v,) = struct.unpack_from("<q", data, offset); offset += 8
            values.append(v)
        else:
            (length,) = struct.unpack_from("<I", data, offset); offset += 4
            values.append(data[offset:offset + length].decode("utf-8")); offset += length
    return values
