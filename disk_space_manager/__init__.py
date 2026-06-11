# disk_space_manager/__init__.py
import os
import struct

from common.results import PageResult, WriteResult


class DiskSpaceManager:
    """Layer 1 — raw page I/O over per-relation `.bin` files.

    Free space tracking (Spec Section 4.1 — "free list or bitmap, your choice")
    uses a per-type **free list** persisted as `<type>_free.bin`. Combined with
    an in-memory high-water mark this guarantees `allocate_page` never reuses a
    page that's been written-but-not-yet-flushed (the bug that bit B+ tree init
    earlier — Spec ISSUE-10 commentary).
    """

    def __init__(self, config: dict):
        self.page_size = config.get("page_size", 4096)
        self.io_reads = 0
        self.io_writes = 0
        # Per-type metadata (loaded lazily on first access).
        self._high_water = {}   # type_name -> next page id to hand out
        self._free_lists = {}   # type_name -> list[page_id]

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def _get_file_path(self, type_name: str) -> str:
        return f"{type_name}.bin"

    def _free_list_path(self, type_name: str) -> str:
        return f"{type_name}_free.bin"

    # ------------------------------------------------------------------
    # Free-space metadata bootstrap (ISSUE-24)
    # ------------------------------------------------------------------
    def _bootstrap(self, type_name: str):
        if type_name in self._high_water:
            return
        # Free list from disk.
        fl_path = self._free_list_path(type_name)
        free_ids: list = []
        if os.path.exists(fl_path):
            with open(fl_path, "rb") as f:
                raw = f.read()
            if raw:
                count = len(raw) // 4
                free_ids = list(struct.unpack(f"!{count}i", raw))
        self._free_lists[type_name] = free_ids
        # High water from file size.
        data_path = self._get_file_path(type_name)
        if os.path.exists(data_path):
            self._high_water[type_name] = os.path.getsize(data_path) // self.page_size
        else:
            self._high_water[type_name] = 0

    def _save_free_list(self, type_name: str):
        fl_path = self._free_list_path(type_name)
        ids = self._free_lists.get(type_name, [])
        with open(fl_path, "wb") as f:
            for pid in ids:
                f.write(struct.pack("!i", pid))

    # ------------------------------------------------------------------
    # Page I/O
    # ------------------------------------------------------------------
    def read_page(self, type_name: str, page_id: int) -> PageResult:
        file_path = self._get_file_path(type_name)
        if not os.path.exists(file_path):
            return PageResult(data=b"\x00" * self.page_size, io_performed=False)
        with open(file_path, "rb") as f:
            f.seek(page_id * self.page_size)
            data = f.read(self.page_size)
            if len(data) < self.page_size:
                data += b"\x00" * (self.page_size - len(data))
        self.io_reads += 1
        return PageResult(data=data, io_performed=True)

    def write_page(self, type_name: str, page_id: int, new_data: bytes) -> WriteResult:
        file_path = self._get_file_path(type_name)
        if len(new_data) != self.page_size:
            new_data = new_data.ljust(self.page_size, b"\x00")

        old_data = b""
        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                f.seek(page_id * self.page_size)
                old_data = f.read(self.page_size)
        if not old_data:
            old_data = b"\x00" * self.page_size

        mode = "r+b" if os.path.exists(file_path) else "wb"
        with open(file_path, mode) as f:
            f.seek(page_id * self.page_size)
            f.write(new_data)

        self.io_writes += 1
        self.log_write(type_name, page_id)

        # Keep the high-water mark in sync: a write may extend the file beyond
        # whatever allocate_page has handed out so far.
        self._bootstrap(type_name)
        if page_id + 1 > self._high_water[type_name]:
            self._high_water[type_name] = page_id + 1
        return WriteResult(success=True, page_id=page_id, old_data=old_data, new_data=new_data)

    # ------------------------------------------------------------------
    # Allocation (ISSUE-24)
    # ------------------------------------------------------------------
    def allocate_page(self, type_name: str) -> int:
        """Hand out a page id. Prefers a recycled id from the free list, else
        bumps the high water mark. Does NOT touch the file — the caller is
        expected to write the page later (typically via the buffer manager)."""
        self._bootstrap(type_name)
        if self._free_lists[type_name]:
            return self._free_lists[type_name].pop()
        new_id = self._high_water[type_name]
        self._high_water[type_name] += 1
        return new_id

    def deallocate_page(self, type_name: str, page_id: int):
        self._bootstrap(type_name)
        if page_id not in self._free_lists[type_name]:
            self._free_lists[type_name].append(page_id)
            self._save_free_list(type_name)

    def reserve_pages(self, type_name: str, up_to_exclusive: int):
        """Used by static hash to mark bucket pages 0..N-1 as taken, so the
        first overflow allocate_page returns hash_buckets onwards rather than
        clobbering bucket 0."""
        self._bootstrap(type_name)
        if up_to_exclusive > self._high_water[type_name]:
            self._high_water[type_name] = up_to_exclusive

    # ------------------------------------------------------------------
    # Stubs / stats
    # ------------------------------------------------------------------
    def log_write(self, type_name: str, page_id: int):
        # Required stub (Spec Section 4.1). Kept cheap.
        pass

    def get_io_stats(self):
        return {"reads": self.io_reads, "writes": self.io_writes}

    def get_stats(self) -> dict:
        return {"reads": self.io_reads, "writes": self.io_writes}

    def reset_stats(self):
        self.io_reads = 0
        self.io_writes = 0

    def append_log(self, log_record_str: str, log_file_path: str = "wal.log"):
        """Recovery Manager'ın logları doğrudan (buffer'ı atlayarak) yazmasını sağlar."""
        with open(log_file_path, "a") as f:
            f.write(log_record_str + "\n")

    def fsync(self, log_file_path: str = "wal.log"):
        """İşletim sistemi seviyesinde buffer'daki logları diske zorlar (Durability)."""
        import os
        with open(log_file_path, "a") as f:
            os.fsync(f.fileno())