"""Validates WAL recording, commit filtering, and crash-recovery
semantics: a committed transaction's writes must survive; an
uncommitted (or truncated mid-write) transaction's writes must not.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loamdb.wal import WAL, WalRecord, BEGIN, INSERT, DELETE, COMMIT, committed_operations


def fresh_path(name):
    if os.path.exists(name):
        os.remove(name)
    return name


def test_committed_transaction_recovers():
    path = fresh_path("test_wal_committed.log")
    wal = WAL(path)
    wal.append(WalRecord(BEGIN, txn_id=1))
    wal.append(WalRecord(INSERT, txn_id=1, table=b"users", key=b"k1", value=b"v1"))
    wal.append(WalRecord(INSERT, txn_id=1, table=b"users", key=b"k2", value=b"v2"))
    wal.commit(1)
    wal.close()

    wal2 = WAL(path)
    ops = committed_operations(wal2.read_all())
    assert ops == [
        (b"users", INSERT, b"k1", b"v1"),
        (b"users", INSERT, b"k2", b"v2"),
    ]
    wal2.close()
    os.remove(path)
    print("test_committed_transaction_recovers passed")


def test_uncommitted_transaction_is_discarded():
    path = fresh_path("test_wal_uncommitted.log")
    wal = WAL(path)
    wal.append(WalRecord(BEGIN, txn_id=1))
    wal.append(WalRecord(INSERT, txn_id=1, table=b"users", key=b"k1", value=b"v1"))
    # crash simulated here: no commit record ever gets written
    wal.close()

    wal2 = WAL(path)
    ops = committed_operations(wal2.read_all())
    assert ops == [], f"expected no ops from an uncommitted transaction, got {ops}"
    wal2.close()
    os.remove(path)
    print("test_uncommitted_transaction_is_discarded passed")


def test_interleaved_transactions_only_committed_ones_replay():
    path = fresh_path("test_wal_interleaved.log")
    wal = WAL(path)
    wal.append(WalRecord(BEGIN, txn_id=1))
    wal.append(WalRecord(INSERT, txn_id=1, table=b"t", key=b"a", value=b"1"))
    wal.append(WalRecord(BEGIN, txn_id=2))
    wal.append(WalRecord(INSERT, txn_id=2, table=b"t", key=b"b", value=b"2"))
    wal.commit(2)  # txn 2 commits...
    wal.append(WalRecord(DELETE, txn_id=1, table=b"t", key=b"a"))
    # ...txn 1 never commits
    wal.close()

    wal2 = WAL(path)
    ops = committed_operations(wal2.read_all())
    assert ops == [(b"t", INSERT, b"b", b"2")], f"got {ops}"
    wal2.close()
    os.remove(path)
    print("test_interleaved_transactions_only_committed_ones_replay passed")


def test_truncated_trailing_record_is_ignored():
    """Simulates a crash that happens mid-write of the LAST record: the
    length prefix says N bytes follow, but the file physically ends
    before N bytes are present. Recovery must treat this as 'nothing more
    to read here', not raise or read garbage."""
    path = fresh_path("test_wal_truncated.log")
    wal = WAL(path)
    wal.append(WalRecord(BEGIN, txn_id=1))
    wal.append(WalRecord(INSERT, txn_id=1, table=b"t", key=b"a", value=b"1"))
    wal.commit(1)
    wal.append(WalRecord(BEGIN, txn_id=2))
    wal.append(WalRecord(INSERT, txn_id=2, table=b"t", key=b"b", value=b"2"))
    wal.close()

    # chop the last 5 bytes off the file to simulate a torn write
    with open(path, "rb") as f:
        data = f.read()
    with open(path, "wb") as f:
        f.write(data[:-5])

    wal2 = WAL(path)
    records = wal2.read_all()
    ops = committed_operations(records)
    assert ops == [(b"t", INSERT, b"a", b"1")], f"got {ops}"
    wal2.close()
    os.remove(path)
    print("test_truncated_trailing_record_is_ignored passed")


def test_checkpoint_clears_the_log():
    path = fresh_path("test_wal_checkpoint.log")
    wal = WAL(path)
    wal.append(WalRecord(BEGIN, txn_id=1))
    wal.commit(1)
    wal.checkpoint()
    assert wal.read_all() == []
    wal.close()
    os.remove(path)
    print("test_checkpoint_clears_the_log passed")


if __name__ == "__main__":
    test_committed_transaction_recovers()
    test_uncommitted_transaction_is_discarded()
    test_interleaved_transactions_only_committed_ones_replay()
    test_truncated_trailing_record_is_ignored()
    test_checkpoint_clears_the_log()
    print("\nALL WAL TESTS PASSED")
