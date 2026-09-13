"""Write-ahead log: every mutation is appended here and fsynced on commit
before the change is trusted to be durable. The actual B+tree pages can
lag behind on disk; what matters is that a committed transaction's log
entries survive a crash and get replayed on the next startup.

Each record is length-prefixed so a partial write from a crash mid-append
(the outer length says N bytes but the file ends before N bytes are
present) is detected as an incomplete trailing record and simply ignored,
rather than misread as garbage. Recovery replays only transactions whose
records end in COMMIT; anything left BEGIN'd but never committed is
discarded, exactly as if it had never happened.
"""
from __future__ import annotations

import os
import struct

BEGIN, INSERT, DELETE, COMMIT = 1, 2, 3, 4


class WalRecord:
    __slots__ = ("kind", "txn_id", "table", "key", "value")

    def __init__(self, kind, txn_id, table=b"", key=b"", value=b""):
        self.kind = kind
        self.txn_id = txn_id
        self.table = table
        self.key = key
        self.value = value


def _encode(record: WalRecord) -> bytes:
    body = bytearray()
    body += struct.pack("<BQ", record.kind, record.txn_id)
    if record.kind in (INSERT, DELETE):
        body += struct.pack("<H", len(record.table))
        body += record.table
        body += struct.pack("<I", len(record.key))
        body += record.key
    if record.kind == INSERT:
        body += struct.pack("<I", len(record.value))
        body += record.value
    return struct.pack("<I", len(body)) + bytes(body)


def _decode_body(body: bytes) -> WalRecord:
    kind, txn_id = struct.unpack_from("<BQ", body, 0)
    offset = 9
    table = key = value = b""
    if kind in (INSERT, DELETE):
        (tlen,) = struct.unpack_from("<H", body, offset); offset += 2
        table = body[offset:offset + tlen]; offset += tlen
        (klen,) = struct.unpack_from("<I", body, offset); offset += 4
        key = body[offset:offset + klen]; offset += klen
    if kind == INSERT:
        (vlen,) = struct.unpack_from("<I", body, offset); offset += 4
        value = body[offset:offset + vlen]; offset += vlen
    return WalRecord(kind, txn_id, table, key, value)


class WAL:
    def __init__(self, path: str):
        self.path = path
        # a+b so existing content is preserved and every write lands at the end
        self._file = open(path, "a+b")

    def append(self, record: WalRecord):
        self._file.write(_encode(record))
        self._file.flush()

    def commit(self, txn_id: int):
        self.append(WalRecord(COMMIT, txn_id))
        self._file.flush()
        os.fsync(self._file.fileno())

    def read_all(self) -> list:
        """Returns every fully-written record in the log, in append order.
        A truncated trailing record (a crash mid-write) is silently
        dropped rather than raising."""
        records = []
        with open(self.path, "rb") as f:
            data = f.read()
        offset = 0
        while offset + 4 <= len(data):
            (length,) = struct.unpack_from("<I", data, offset)
            offset += 4
            if offset + length > len(data):
                break  # partial trailing record from a crash mid-append -- stop here
            records.append(_decode_body(data[offset:offset + length]))
            offset += length
        return records

    def checkpoint(self):
        """Called once every committed record has been durably applied to
        the actual table pages -- the log can be safely discarded since
        replaying it again would be redundant, not just harmless."""
        self._file.close()
        os.remove(self.path)
        self._file = open(self.path, "a+b")

    def close(self):
        self._file.close()


def committed_operations(records: list) -> list:
    """Filters a raw record list down to (table, kind, key, value) tuples
    from transactions that actually reached COMMIT, in the order their
    owning transaction committed -- exactly what recovery should replay."""
    by_txn = {}
    order = []
    for r in records:
        if r.txn_id not in by_txn:
            by_txn[r.txn_id] = []
        by_txn[r.txn_id].append(r)
        if r.kind == COMMIT:
            order.append(r.txn_id)

    ops = []
    for txn_id in order:
        for r in by_txn[txn_id]:
            if r.kind in (INSERT, DELETE):
                ops.append((r.table, r.kind, r.key, r.value))
    return ops
