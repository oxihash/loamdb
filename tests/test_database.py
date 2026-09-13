"""Validates the Database/Transaction layer, including a real crash
simulation: WAL records are written and fsynced exactly as a commit
would, but the page file is never touched and the process is
"killed" (the Database object is just dropped) before recovery would
normally run -- then a fresh Database over the same files must recover
the committed data from the WAL alone.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loamdb.database import Database
from loamdb.wal import WAL, WalRecord, BEGIN, INSERT


def cleanup(path):
    for ext in ("", ".wal"):
        p = path + ext
        if os.path.exists(p):
            os.remove(p)


def test_basic_crud_and_transactions():
    path = "test_db_basic.db"
    cleanup(path)
    db = Database(path)

    with db.begin() as txn:
        txn.insert(b"users", b"1", b"alice")
        txn.insert(b"users", b"2", b"bob")

    assert db.table_btree(b"users").get(b"1") == b"alice"
    assert db.table_btree(b"users").get(b"2") == b"bob"

    with db.begin() as txn:
        assert txn.get(b"users", b"1") == b"alice"  # read committed data
        txn.insert(b"users", b"1", b"alice-updated")
        assert txn.get(b"users", b"1") == b"alice-updated"  # read own uncommitted write

    assert db.table_btree(b"users").get(b"1") == b"alice-updated"

    db.close()
    cleanup(path)
    print("test_basic_crud_and_transactions passed")


def test_rollback_discards_writes():
    path = "test_db_rollback.db"
    cleanup(path)
    db = Database(path)

    with db.begin() as txn:
        txn.insert(b"users", b"1", b"alice")

    try:
        with db.begin() as txn:
            txn.insert(b"users", b"1", b"should-not-stick")
            raise ValueError("simulated failure mid-transaction")
    except ValueError:
        pass

    assert db.table_btree(b"users").get(b"1") == b"alice", "rollback should have discarded the second write"
    db.close()
    cleanup(path)
    print("test_rollback_discards_writes passed")


def test_multiple_tables_independent():
    path = "test_db_multitable.db"
    cleanup(path)
    db = Database(path)

    with db.begin() as txn:
        txn.insert(b"users", b"1", b"alice")
        txn.insert(b"products", b"1", b"widget")

    assert db.table_btree(b"users").get(b"1") == b"alice"
    assert db.table_btree(b"products").get(b"1") == b"widget"
    assert set(db.list_tables()) == {b"users", b"products"}
    db.close()
    cleanup(path)
    print("test_multiple_tables_independent passed")


def test_crash_recovery_replays_committed_transaction():
    path = "test_db_crash.db"
    cleanup(path)

    # First, establish a real table via a normal transaction so the
    # catalog and table root already exist on disk.
    db = Database(path)
    with db.begin() as txn:
        txn.insert(b"users", b"1", b"alice")
    db.close()

    # Now simulate a crash: write WAL records for a SECOND transaction
    # exactly as Transaction.commit() would (BEGIN, INSERT, then a
    # fsynced COMMIT), but never touch the actual table pages and never
    # call Database._recover() in this process -- the equivalent of the
    # process dying right after the commit fsync returns.
    wal = WAL(path + ".wal")
    wal.append(WalRecord(BEGIN, txn_id=999))
    wal.append(WalRecord(INSERT, txn_id=999, table=b"users", key=b"2", value=b"bob"))
    wal.commit(999)
    wal.close()

    # A fresh Database over the same files must recover "bob" purely from
    # the WAL -- it was never written to the users table's pages before
    # the simulated crash.
    db2 = Database(path)
    assert db2.table_btree(b"users").get(b"1") == b"alice"
    assert db2.table_btree(b"users").get(b"2") == b"bob", "crash recovery did not replay the committed transaction"
    db2.close()
    cleanup(path)
    print("test_crash_recovery_replays_committed_transaction passed")


def test_crash_recovery_discards_uncommitted_transaction():
    path = "test_db_crash_uncommitted.db"
    cleanup(path)
    db = Database(path)
    with db.begin() as txn:
        txn.insert(b"users", b"1", b"alice")
    db.close()

    # Simulate a crash mid-transaction: BEGIN and INSERT are logged, but
    # the process dies before COMMIT is ever written.
    wal = WAL(path + ".wal")
    wal.append(WalRecord(BEGIN, txn_id=999))
    wal.append(WalRecord(INSERT, txn_id=999, table=b"users", key=b"2", value=b"bob"))
    wal.close()

    db2 = Database(path)
    assert db2.table_btree(b"users").get(b"1") == b"alice"
    assert db2.table_btree(b"users").get(b"2") is None, "an uncommitted transaction's write should not have survived recovery"
    db2.close()
    cleanup(path)
    print("test_crash_recovery_discards_uncommitted_transaction passed")


def test_persists_across_many_reopens_with_growth():
    """Inserts enough rows to force several root splits on both the
    table's own B+tree and the catalog's, closing and reopening the
    database partway through, to check that catalog-root bookkeeping
    survives a real restart under real growth, not just a
    same-process check."""
    path = "test_db_growth.db"
    cleanup(path)
    db = Database(path)
    with db.begin() as txn:
        for i in range(400):
            txn.insert(b"big_table", f"k{i:05d}".encode(), (f"v{i}" * 20).encode())
    db.close()

    db2 = Database(path)
    with db2.begin() as txn:
        for i in range(400, 800):
            txn.insert(b"big_table", f"k{i:05d}".encode(), (f"v{i}" * 20).encode())
    db2.close()

    db3 = Database(path)
    bt = db3.table_btree(b"big_table")
    for i in range(800):
        assert bt.get(f"k{i:05d}".encode()) == (f"v{i}" * 20).encode(), f"missing/wrong value at k{i:05d}"
    assert len(list(bt.scan())) == 800
    db3.close()
    cleanup(path)
    print("test_persists_across_many_reopens_with_growth passed")


if __name__ == "__main__":
    test_basic_crud_and_transactions()
    test_rollback_discards_writes()
    test_multiple_tables_independent()
    test_crash_recovery_replays_committed_transaction()
    test_crash_recovery_discards_uncommitted_transaction()
    test_persists_across_many_reopens_with_growth()
    print("\nALL DATABASE TESTS PASSED")
