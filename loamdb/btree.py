"""A B+tree keyed on raw bytes, stored one node per page via the Pager.

Leaf nodes hold the actual key/value pairs and are threaded together with
a next-leaf pointer for cheap ordered range scans. Internal nodes hold
routing keys and child page numbers only, never values.

A node is fully deserialized into a plain Python object, mutated, and
reserialized on every operation rather than edited in place as raw bytes.
That costs some performance but keeps the logic short enough to actually
verify by reading it, which matters more here than raw throughput.

Splitting is by key COUNT at the midpoint, not by exact byte size, so an
insert that would overflow a page splits it in two before writing.
Deletion removes a key from its leaf but never merges underflowing
leaves back together: an emptied leaf is simply left in place (still
linked into the leaf chain, still reachable from its parent) and skipped
by scans and future inserts recognize it as normal, and just insert. This
is a deliberate simplification, documented in the README, that trades a
small amount of space efficiency under heavy delete-then-reinsert
workloads for a much smaller, easier-to-verify deletion path with no
rebalancing-on-underflow logic to get subtly wrong.
"""
from __future__ import annotations

import struct

from .pager import PAGE_SIZE, Pager

LEAF = 1
INTERNAL = 2


class BTreeError(Exception):
    pass


class LeafNode:
    __slots__ = ("keys", "values", "next_leaf", "page_num")

    def __init__(self, keys, values, next_leaf=0, page_num=None):
        self.keys = keys
        self.values = values
        self.next_leaf = next_leaf
        self.page_num = page_num


class InternalNode:
    __slots__ = ("keys", "children", "page_num")

    def __init__(self, keys, children, page_num=None):
        self.keys = keys          # len(keys) == len(children) - 1
        self.children = children  # page numbers
        self.page_num = page_num


def _serialize_leaf(node: LeafNode) -> bytes:
    out = bytearray()
    out += struct.pack("<BHQ", LEAF, len(node.keys), node.next_leaf)
    for k, v in zip(node.keys, node.values):
        out += struct.pack("<HH", len(k), len(v))
        out += k
        out += v
    if len(out) > PAGE_SIZE:
        raise BTreeError("leaf node serialized larger than one page -- caller must split before writing")
    return bytes(out).ljust(PAGE_SIZE, b"\x00")


def _serialize_internal(node: InternalNode) -> bytes:
    out = bytearray()
    out += struct.pack("<BHQ", INTERNAL, len(node.keys), node.children[0])
    for k, child in zip(node.keys, node.children[1:]):
        out += struct.pack("<H", len(k))
        out += k
        out += struct.pack("<Q", child)
    if len(out) > PAGE_SIZE:
        raise BTreeError("internal node serialized larger than one page -- caller must split before writing")
    return bytes(out).ljust(PAGE_SIZE, b"\x00")


def _deserialize(data: bytes, page_num: int):
    node_type, count, first = struct.unpack_from("<BHQ", data, 0)
    offset = 11
    if node_type == LEAF:
        keys, values = [], []
        for _ in range(count):
            klen, vlen = struct.unpack_from("<HH", data, offset)
            offset += 4
            keys.append(data[offset:offset + klen]); offset += klen
            values.append(data[offset:offset + vlen]); offset += vlen
        return LeafNode(keys, values, next_leaf=first, page_num=page_num)
    elif node_type == INTERNAL:
        keys, children = [], [first]
        for _ in range(count):
            (klen,) = struct.unpack_from("<H", data, offset)
            offset += 2
            keys.append(data[offset:offset + klen]); offset += klen
            (child,) = struct.unpack_from("<Q", data, offset)
            offset += 8
            children.append(child)
        return InternalNode(keys, children, page_num=page_num)
    raise BTreeError(f"page {page_num}: unknown node type byte {node_type}")


class BTree:
    """One BTree instance per root page number -- a loamdb file typically
    holds many of these (one per table, one per index), all sharing the
    same underlying Pager/file."""

    def __init__(self, pager: Pager, root_page: int = 0):
        self.pager = pager
        self.root_page = root_page
        if self.root_page == 0:
            leaf = LeafNode(keys=[], values=[], next_leaf=0)
            self.root_page = self.pager.allocate_page()
            leaf.page_num = self.root_page
            self._write(leaf)

    def _read(self, page_num: int):
        return _deserialize(self.pager.read_page(page_num), page_num)

    def _write(self, node):
        data = _serialize_leaf(node) if isinstance(node, LeafNode) else _serialize_internal(node)
        self.pager.write_page(node.page_num, data)

    # ---- point lookups ----

    def get(self, key: bytes):
        leaf = self._find_leaf(key)
        for k, v in zip(leaf.keys, leaf.values):
            if k == key:
                return v
        return None

    def _find_leaf(self, key: bytes) -> LeafNode:
        node = self._read(self.root_page)
        while isinstance(node, InternalNode):
            child_idx = 0
            while child_idx < len(node.keys) and key >= node.keys[child_idx]:
                child_idx += 1
            node = self._read(node.children[child_idx])
        return node

    # ---- ordered range scan ----

    def scan(self, start: bytes = None, end: bytes = None):
        """Yields (key, value) pairs with start <= key <= end (either
        bound optional) in ascending key order."""
        leaf = self._read(self.root_page)
        while isinstance(leaf, InternalNode):
            child_idx = 0
            if start is not None:
                while child_idx < len(leaf.keys) and start >= leaf.keys[child_idx]:
                    child_idx += 1
            leaf = self._read(leaf.children[child_idx])
        while leaf is not None:
            for k, v in zip(leaf.keys, leaf.values):
                if start is not None and k < start:
                    continue
                if end is not None and k > end:
                    return
                yield k, v
            leaf = self._read(leaf.next_leaf) if leaf.next_leaf else None

    def __iter__(self):
        return self.scan()

    # ---- insert ----

    def insert(self, key: bytes, value: bytes):
        path = self._find_path(key)
        leaf = path[-1]
        if key in leaf.keys:
            leaf.values[leaf.keys.index(key)] = value
            self._write(leaf)
            return
        idx = _bisect(leaf.keys, key)
        leaf.keys.insert(idx, key)
        leaf.values.insert(idx, value)
        self._split_and_propagate(leaf, path[:-1])

    def _find_path(self, key: bytes) -> list:
        """Returns [root, ..., leaf] -- every internal node walked through,
        needed so an overflowing split can propagate a new separator key
        up to the correct parent."""
        path = []
        node = self._read(self.root_page)
        path.append(node)
        while isinstance(node, InternalNode):
            child_idx = 0
            while child_idx < len(node.keys) and key >= node.keys[child_idx]:
                child_idx += 1
            node = self._read(node.children[child_idx])
            path.append(node)
        return path

    def _split_and_propagate(self, node, ancestors: list):
        try:
            self._write(node)
            return  # fits, no split needed
        except BTreeError:
            pass

        mid = len(node.keys) // 2
        if isinstance(node, LeafNode):
            right = LeafNode(
                keys=node.keys[mid:], values=node.values[mid:],
                next_leaf=node.next_leaf, page_num=self.pager.allocate_page(),
            )
            node.keys, node.values = node.keys[:mid], node.values[:mid]
            node.next_leaf = right.page_num
            separator = right.keys[0]
        else:
            # An internal split promotes its middle key up instead of
            # copying it into both halves, the standard B+tree rule (only
            # leaves duplicate a copy of the separator; internal nodes don't
            # store values, so keeping it in the right half is redundant).
            promoted = node.keys[mid]
            right = InternalNode(
                keys=node.keys[mid + 1:], children=node.children[mid + 1:],
                page_num=self.pager.allocate_page(),
            )
            node.keys, node.children = node.keys[:mid], node.children[:mid + 1]
            separator = promoted

        self._write(node)
        self._write(right)

        if not ancestors:
            new_root = InternalNode(keys=[separator], children=[node.page_num, right.page_num],
                                     page_num=self.pager.allocate_page())
            self._write(new_root)
            self.root_page = new_root.page_num  # callers that persist root pages elsewhere (the catalog) must re-check this after any insert
            return

        parent = ancestors[-1]
        idx = _bisect(parent.keys, separator)
        parent.keys.insert(idx, separator)
        parent.children.insert(idx + 1, right.page_num)
        self._split_and_propagate(parent, ancestors[:-1])

    # ---- delete ----

    def delete(self, key: bytes) -> bool:
        leaf = self._find_leaf(key)
        if key not in leaf.keys:
            return False
        idx = leaf.keys.index(key)
        del leaf.keys[idx]
        del leaf.values[idx]
        self._write(leaf)  # deleting only ever shrinks a page, so this can never overflow
        return True


def _bisect(sorted_keys: list, key: bytes) -> int:
    lo, hi = 0, len(sorted_keys)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_keys[mid] < key:
            lo = mid + 1
        else:
            hi = mid
    return lo
