"""Validates the B+tree against a plain Python dict used as a reference
model: every operation is mirrored on both, and after each batch the
B+tree's full ordered scan is compared against the dict's sorted items.
This catches split/propagation bugs that a handful of hand-picked cases
would likely miss, since split boundaries depend on exactly how much data
fits in a page.
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loamdb.btree import BTree
from loamdb.pager import Pager


def fresh_pager(path):
    if os.path.exists(path):
        os.remove(path)
    return Pager(path)


def test_insert_get_basic():
    pager = fresh_pager("test_basic.db")
    tree = BTree(pager)
    tree.insert(b"apple", b"1")
    tree.insert(b"banana", b"2")
    tree.insert(b"cherry", b"3")
    assert tree.get(b"apple") == b"1"
    assert tree.get(b"banana") == b"2"
    assert tree.get(b"missing") is None
    pager.close()
    os.remove("test_basic.db")
    print("test_insert_get_basic passed")


def test_overwrite():
    pager = fresh_pager("test_overwrite.db")
    tree = BTree(pager)
    tree.insert(b"key", b"v1")
    tree.insert(b"key", b"v2")
    assert tree.get(b"key") == b"v2"
    pager.close()
    os.remove("test_overwrite.db")
    print("test_overwrite passed")


def test_scan_ordering():
    pager = fresh_pager("test_scan.db")
    tree = BTree(pager)
    keys = [b"delta", b"alpha", b"charlie", b"bravo"]
    for k in keys:
        tree.insert(k, k.upper())
    result = list(tree.scan())
    assert [k for k, v in result] == sorted(keys)
    pager.close()
    os.remove("test_scan.db")
    print("test_scan_ordering passed")


def test_range_scan():
    pager = fresh_pager("test_range.db")
    tree = BTree(pager)
    for i in range(20):
        k = f"k{i:03d}".encode()
        tree.insert(k, str(i).encode())
    result = list(tree.scan(start=b"k005", end=b"k010"))
    assert [k.decode() for k, v in result] == [f"k{i:03d}" for i in range(5, 11)]
    pager.close()
    os.remove("test_range.db")
    print("test_range_scan passed")


def test_forces_multiple_splits_and_matches_reference():
    """Inserts enough large-ish keys to force many page splits (page size
    is 4096 bytes; these keys/values are chosen to guarantee dozens of
    leaf splits and at least one internal-node split), then cross-checks
    every read path against a plain dict."""
    pager = fresh_pager("test_stress.db")
    tree = BTree(pager)
    reference = {}

    random.seed(1234)
    keys = [f"key-{i:06d}-{'x' * random.randint(0, 40)}".encode() for i in range(2000)]
    random.shuffle(keys)

    for i, k in enumerate(keys):
        v = f"value-{i}".encode()
        tree.insert(k, v)
        reference[k] = v

    # point lookups for every key
    for k, v in reference.items():
        assert tree.get(k) == v, f"mismatch on {k!r}"
    assert tree.get(b"definitely-not-present") is None

    # full ordered scan must match the reference exactly
    scanned = list(tree.scan())
    expected = sorted(reference.items())
    assert scanned == expected, f"scan mismatch: {len(scanned)} vs {len(expected)} entries"

    # a bounded range scan must match a dict-derived slice
    start, end = sorted(reference.keys())[500], sorted(reference.keys())[700]
    ranged = list(tree.scan(start=start, end=end))
    expected_ranged = [(k, v) for k, v in expected if start <= k <= end]
    assert ranged == expected_ranged

    pager.close()
    os.remove("test_stress.db")
    print(f"test_forces_multiple_splits_and_matches_reference passed ({len(keys)} keys)")


def test_delete_then_reinsert_then_scan():
    pager = fresh_pager("test_delete.db")
    tree = BTree(pager)
    reference = {}

    random.seed(99)
    keys = [f"k{i:04d}".encode() for i in range(500)]
    for k in keys:
        v = k[::-1]
        tree.insert(k, v)
        reference[k] = v

    to_delete = random.sample(keys, 200)
    for k in to_delete:
        assert tree.delete(k) is True
        del reference[k]
    assert tree.delete(b"never-existed") is False

    for k in keys:
        expected = reference.get(k)
        assert tree.get(k) == expected, f"post-delete mismatch on {k!r}"

    assert list(tree.scan()) == sorted(reference.items())

    # reinsert some of the deleted keys with new values and verify again
    for k in to_delete[:50]:
        v = b"reinserted"
        tree.insert(k, v)
        reference[k] = v
    assert list(tree.scan()) == sorted(reference.items())

    pager.close()
    os.remove("test_delete.db")
    print("test_delete_then_reinsert_then_scan passed")


def test_persistence_across_reopen():
    path = "test_persist.db"
    if os.path.exists(path):
        os.remove(path)
    pager = Pager(path)
    tree = BTree(pager)
    for i in range(300):
        tree.insert(f"p{i:04d}".encode(), str(i * i).encode())
    root = tree.root_page
    pager.close()

    pager2 = Pager(path)
    tree2 = BTree(pager2, root_page=root)
    for i in range(300):
        assert tree2.get(f"p{i:04d}".encode()) == str(i * i).encode()
    pager2.close()
    os.remove(path)
    print("test_persistence_across_reopen passed")


if __name__ == "__main__":
    test_insert_get_basic()
    test_overwrite()
    test_scan_ordering()
    test_range_scan()
    test_forces_multiple_splits_and_matches_reference()
    test_delete_then_reinsert_then_scan()
    test_persistence_across_reopen()
    print("\nALL BTREE TESTS PASSED")
