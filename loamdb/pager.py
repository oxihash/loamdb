"""Fixed-size page storage on top of a single file.

Every other layer in loamdb (the B+tree, the WAL, the catalog) reads and
writes whole pages through this module and never touches file offsets
directly. Page 0 is reserved for the file header: a magic number, the
total page count, the head of the free list, and the root page number of
the catalog B+tree (every other table's root is looked up through the
catalog, so this is the only root loamdb needs to remember directly).

Freed pages are kept in a singly linked list threaded through the pages
themselves: the first 8 bytes of a free page hold the page number of the
next free page (0 means end of list). This means freeing and reusing a
page costs one page write each way, no separate free-list structure to
keep in sync.
"""
from __future__ import annotations

import os
import struct

PAGE_SIZE = 4096
HEADER_MAGIC = b"LOAMDB01"
HEADER_PAGE = 0

# Header layout (page 0): magic(8s) page_count(Q) free_list_head(Q) catalog_root(Q)
_HEADER_FMT = "<8sQQQ"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


class CorruptPageError(Exception):
    pass


class Pager:
    def __init__(self, path: str):
        self.path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._file = open(path, "r+b" if not is_new else "w+b")
        if is_new:
            self._init_new_file()
        else:
            self._read_header()

    def _init_new_file(self):
        self.page_count = 1  # page 0 is the header itself
        self.free_list_head = 0
        self.catalog_root = 0  # 0 means "not yet allocated"
        self._write_header()
        self._file.flush()
        os.fsync(self._file.fileno())

    def _read_header(self):
        self._file.seek(0)
        raw = self._file.read(PAGE_SIZE)
        if len(raw) < _HEADER_SIZE:
            raise CorruptPageError(f"'{self.path}': file too short to contain a valid header")
        magic, page_count, free_head, catalog_root = struct.unpack(_HEADER_FMT, raw[:_HEADER_SIZE])
        if magic != HEADER_MAGIC:
            raise CorruptPageError(f"'{self.path}': bad magic bytes {magic!r}, not a loamdb file")
        self.page_count = page_count
        self.free_list_head = free_head
        self.catalog_root = catalog_root

    def _write_header(self):
        raw = struct.pack(_HEADER_FMT, HEADER_MAGIC, self.page_count, self.free_list_head, self.catalog_root)
        raw = raw.ljust(PAGE_SIZE, b"\x00")
        self._file.seek(0)
        self._file.write(raw)

    def set_catalog_root(self, page_num: int):
        self.catalog_root = page_num
        self._write_header()

    def read_page(self, page_num: int) -> bytes:
        if page_num <= 0 or page_num >= self.page_count:
            raise CorruptPageError(f"read_page({page_num}): out of range (page_count={self.page_count})")
        self._file.seek(page_num * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise CorruptPageError(f"read_page({page_num}): short read ({len(data)} bytes)")
        return data

    def write_page(self, page_num: int, data: bytes):
        if len(data) != PAGE_SIZE:
            raise ValueError(f"write_page({page_num}): data must be exactly {PAGE_SIZE} bytes, got {len(data)}")
        if page_num <= 0 or page_num >= self.page_count:
            raise CorruptPageError(f"write_page({page_num}): out of range (page_count={self.page_count})")
        self._file.seek(page_num * PAGE_SIZE)
        self._file.write(data)

    def allocate_page(self) -> int:
        """Returns a new, zeroed page: reuses one from the free list if
        available, otherwise extends the file."""
        if self.free_list_head != 0:
            page_num = self.free_list_head
            raw = self._raw_read(page_num)
            (next_free,) = struct.unpack_from("<Q", raw, 0)
            self.free_list_head = next_free
            self._write_header()
            self._raw_write(page_num, b"\x00" * PAGE_SIZE)
            return page_num
        page_num = self.page_count
        self.page_count += 1
        self._raw_write(page_num, b"\x00" * PAGE_SIZE)
        self._write_header()
        return page_num

    def free_page(self, page_num: int):
        raw = bytearray(PAGE_SIZE)
        struct.pack_into("<Q", raw, 0, self.free_list_head)
        self._raw_write(page_num, bytes(raw))
        self.free_list_head = page_num
        self._write_header()

    def _raw_read(self, page_num: int) -> bytes:
        self._file.seek(page_num * PAGE_SIZE)
        return self._file.read(PAGE_SIZE)

    def _raw_write(self, page_num: int, data: bytes):
        self._file.seek(page_num * PAGE_SIZE)
        self._file.write(data)

    def flush(self):
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self):
        self.flush()
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
