"""Ties the pager, the B+tree, and the WAL into something that behaves
like a real embedded database: named tables, transactions, and crash
recovery on open.

Table roots are themselves stored in a B+tree (the catalog), so adding a
table or growing one enough to split its root is just another write, no
separate metadata file. The catalog's own root page is the one thing
that has to live outside any B+tree, so it's stored directly in the
pager's file header.

Concurrency model: a single writer at a time, guarded by a plain
threading.Lock, with the WAL commit as the durability boundary. This is
not MVCC. It's the same trade a lot of production embedded databases
(SQLite in its default rollback-journal mode, for instance) make: give up
concurrent writers in exchange for a transaction model simple enough to
verify is actually correct, rather than a snapshot-isolation
implementation with subtle bugs no one has found yet.
"""
from __future__ import annotations

import struct
import threading

from .btree import BTree
from .pager import Pager
from .wal import WAL, WalRecord, BEGIN, INSERT, DELETE, committed_operations


class Database:
    def __init__(self, path: str):
        self.pager = Pager(path)
        self.wal = WAL(path + ".wal")
        self._lock = threading.Lock()
        self._catalog = BTree(self.pager, root_page=self.pager.catalog_root)
        if self._catalog.root_page != self.pager.catalog_root:
            self.pager.set_catalog_root(self._catalog.root_page)
        self._tables = {}       # name -> BTree
        self._table_roots = {}  # name -> last root page persisted in the catalog
        self._next_txn_id = 1
        self._recover()

    def _recover(self):
        """Replays every committed-but-not-yet-checkpointed operation from
        the WAL. Safe to call on an empty log (does nothing) so a normal,
        clean startup and a post-crash startup go through the exact same
        code path -- recovery isn't a special case bolted on separately."""
        ops = committed_operations(self.wal.read_all())
        for table, kind, key, value in ops:
            self._apply(table, kind, key, value)
        self.pager.flush()
        self.wal.checkpoint()

    def table_btree(self, name: bytes) -> BTree:
        if name in self._tables:
            return self._tables[name]
        root_bytes = self._catalog.get(b"table:" + name)
        root = struct.unpack("<Q", root_bytes)[0] if root_bytes else 0
        bt = BTree(self.pager, root_page=root)
        self._tables[name] = bt
        self._table_roots[name] = bt.root_page
        if root == 0:
            self._persist_catalog_entry(name, bt)
        return bt

    def get_schema(self, table_name: bytes):
        return self._catalog.get(b"schema:" + table_name)

    def set_schema(self, table_name: bytes, schema_bytes: bytes):
        self._catalog.insert(b"schema:" + table_name, schema_bytes)
        if self._catalog.root_page != self.pager.catalog_root:
            self.pager.set_catalog_root(self._catalog.root_page)

    def _apply(self, table: bytes, kind: int, key: bytes, value: bytes):
        bt = self.table_btree(table)
        if kind == INSERT:
            bt.insert(key, value)
        else:
            bt.delete(key)
        if bt.root_page != self._table_roots[table]:
            self._persist_catalog_entry(table, bt)

    def _persist_catalog_entry(self, table: bytes, bt: BTree):
        self._catalog.insert(b"table:" + table, struct.pack("<Q", bt.root_page))
        self._table_roots[table] = bt.root_page
        if self._catalog.root_page != self.pager.catalog_root:
            self.pager.set_catalog_root(self._catalog.root_page)

    def list_tables(self) -> list:
        return [name[len(b"table:"):] for name, _ in self._catalog.scan() if name.startswith(b"table:")]

    def begin(self) -> "Transaction":
        with self._lock:
            txn_id = self._next_txn_id
            self._next_txn_id += 1
        txn = Transaction(self, txn_id)
        self._lock.acquire()  # released on commit()/rollback() -- single-writer model, see module docstring
        self.wal.append(WalRecord(BEGIN, txn_id))
        return txn

    def close(self):
        self.pager.close()
        self.wal.close()


class Transaction:
    def __init__(self, db: Database, txn_id: int):
        self.db = db
        self.txn_id = txn_id
        self._pending = []
        self._done = False

    def insert(self, table: bytes, key: bytes, value: bytes):
        self._check_open()
        self.db.wal.append(WalRecord(INSERT, self.txn_id, table, key, value))
        self._pending.append((table, INSERT, key, value))

    def delete(self, table: bytes, key: bytes):
        self._check_open()
        self.db.wal.append(WalRecord(DELETE, self.txn_id, table, key))
        self._pending.append((table, DELETE, key, b""))

    def get(self, table: bytes, key: bytes):
        """Reads see this transaction's own uncommitted writes first (so a
        transaction can read back what it just wrote), falling through to
        the committed table state otherwise."""
        for t, kind, k, v in reversed(self._pending):
            if t == table and k == key:
                return v if kind == INSERT else None
        return self.db.table_btree(table).get(key)

    def commit(self):
        self._check_open()
        self.db.wal.commit(self.txn_id)  # fsync here is the actual durability point
        for table, kind, key, value in self._pending:
            self.db._apply(table, kind, key, value)
        self.db.pager.flush()
        self._finish()

    def rollback(self):
        """The transaction's writes were logged but never committed, so
        they were never applied to the actual table pages and will never
        be replayed by recovery either -- there's nothing to undo beyond
        just dropping the pending list and releasing the writer lock."""
        self._check_open()
        self._pending = []
        self._finish()

    def _check_open(self):
        if self._done:
            raise RuntimeError("transaction already committed or rolled back")

    def _finish(self):
        self._done = True
        self.db._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._done:
            self.rollback() if exc_type else self.commit()
