# file_index_manager/__init__.py
import struct
import hashlib

from common.results import RecordResult, InsertResult, DeleteResult


class FileIndexManager:
    # ----- system catalog constants (ISSUE-04) -----
    CATALOG_NAME = "_catalog_"
    CATALOG_TYPE_NAME_BYTES = 20
    CATALOG_FIELD_NAME_BYTES = 20
    FTYPE_INT = 0
    FTYPE_STR = 1

    # ----- record encoding widths (chosen here to match QP's pack format) -----
    INT_KEY_BYTES = 4
    STR_KEY_BYTES = 20

    # ----- slotted page layout (ISSUE-23 — bitmap occupancy) -----
    #   !H num_slots
    #   !H free_space_end
    #   !H slot_bitmap   (16-bit; supports max_records_per_page <= 16)
    #   !HH × num_slots  slot directory (rec_offset, rec_len)
    SLOTTED_HEADER_FMT = "!HHH"
    SLOTTED_HEADER_SIZE = 6
    SLOT_ENTRY_SIZE = 4
    SLOT_ENTRY_FMT = "!HH"

    def __init__(self, config: dict, buffer):
        self.buffer = buffer
        self.page_size = config.get("page_size", 4096)
        self.index_strategy = config.get("index_strategy", "heap_scan")  # heap_scan, hash_index, bplus_tree
        self.max_records_per_page = config.get("max_records_per_page", 10)
        self.hash_buckets = 10  # static hash bucket count
        # Cumulative since last `stats reset` (Spec Section 9.3).
        self.index_nodes_visited = 0
        # First-time setup guards.
        self._hash_initialized = set()

        # WAL 
        self.recovery_manager = None
        self.current_xid = None # QueryProcessor burayı transaction süresince doldurur

    # ------------------------------------------------------------------
    # Slotted-page header helpers (ISSUE-23)
    # ------------------------------------------------------------------
    def _read_slot_header(self, data) -> tuple:
        return struct.unpack_from(self.SLOTTED_HEADER_FMT, data, 0)

    def _write_slot_header(self, data: bytearray, num_slots: int, free_space_end: int, bitmap: int):
        struct.pack_into(self.SLOTTED_HEADER_FMT, data, 0, num_slots, free_space_end, bitmap)

    def _slot_dir_offset(self, slot_idx: int) -> int:
        return self.SLOTTED_HEADER_SIZE + slot_idx * self.SLOT_ENTRY_SIZE

    @staticmethod
    def _slot_live(bitmap: int, idx: int) -> bool:
        return bool(bitmap & (1 << idx))

    @staticmethod
    def _set_slot(bitmap: int, idx: int) -> int:
        return bitmap | (1 << idx)

    @staticmethod
    def _clear_slot(bitmap: int, idx: int) -> int:
        return bitmap & ~(1 << idx)

    # ------------------------------------------------------------------
    # System catalog (ISSUE-04 + ISSUE-11) — page 0 of `_catalog_.bin`
    # ------------------------------------------------------------------
    def _load_catalog(self) -> list:
        buf = self.buffer.fetch_page(self.CATALOG_NAME, 0)
        data = buf.page.data
        types = []
        if all(b == 0 for b in data[:2]):
            return types
        num_types, = struct.unpack_from("!H", data, 0)
        off = 2
        for _ in range(num_types):
            name_bytes, num_fields, pk_order = struct.unpack_from(
                f"!{self.CATALOG_TYPE_NAME_BYTES}sBB", data, off
            )
            off += self.CATALOG_TYPE_NAME_BYTES + 2
            fields = []
            for _ in range(num_fields):
                fname_bytes, ftype_code = struct.unpack_from(
                    f"!{self.CATALOG_FIELD_NAME_BYTES}sB", data, off
                )
                off += self.CATALOG_FIELD_NAME_BYTES + 1
                fname = fname_bytes.decode("utf-8").rstrip("\x00")
                ftype = "int" if ftype_code == self.FTYPE_INT else "str"
                fields.append((fname, ftype))
            type_name = name_bytes.decode("utf-8").rstrip("\x00")
            types.append({
                "name": type_name,
                "num_fields": num_fields,
                "pk_order": pk_order,
                "fields": fields,
            })
        return types

    def _save_catalog(self, types: list) -> bool:
        data = bytearray(self.page_size)
        struct.pack_into("!H", data, 0, len(types))
        off = 2
        for t in types:
            name_bytes = t["name"].encode("utf-8").ljust(self.CATALOG_TYPE_NAME_BYTES, b"\x00")
            entry_size = self.CATALOG_TYPE_NAME_BYTES + 2 + t["num_fields"] * (self.CATALOG_FIELD_NAME_BYTES + 1)
            if off + entry_size > self.page_size:
                return False
            struct.pack_into(
                f"!{self.CATALOG_TYPE_NAME_BYTES}sBB",
                data, off, name_bytes, t["num_fields"], t["pk_order"],
            )
            off += self.CATALOG_TYPE_NAME_BYTES + 2
            for fname, ftype in t["fields"]:
                fname_bytes = fname.encode("utf-8").ljust(self.CATALOG_FIELD_NAME_BYTES, b"\x00")
                code = self.FTYPE_INT if ftype == "int" else self.FTYPE_STR
                struct.pack_into(
                    f"!{self.CATALOG_FIELD_NAME_BYTES}sB",
                    data, off, fname_bytes, code,
                )
                off += self.CATALOG_FIELD_NAME_BYTES + 1
        self.log_and_put_page(self.CATALOG_NAME, 0, bytes(data))
        return True

    def catalog_create_type(self, name: str, num_fields: int, pk_order: int, fields: list) -> bool:
        types = self._load_catalog()
        if any(t["name"] == name for t in types):
            return False
        types.append({
            "name": name,
            "num_fields": num_fields,
            "pk_order": pk_order,
            "fields": fields,
        })
        return self._save_catalog(types)

    def catalog_get_type(self, name: str):
        for t in self._load_catalog():
            if t["name"] == name:
                return t
        return None

    def catalog_list_types(self) -> list:
        return [t["name"] for t in self._load_catalog()]

    def catalog_drop_type(self, name: str) -> bool:
        """Recovery undo'su loser bir create type'i geri alirken kullanir."""
        types = self._load_catalog()
        remaining = [t for t in types if t["name"] != name]
        if len(remaining) == len(types):
            return False
        return self._save_catalog(remaining)

    # ------------------------------------------------------------------
    # Stats (ISSUE-08)
    # ------------------------------------------------------------------
    def get_stats(self) -> dict:
        return {
            "index_strategy": self.index_strategy,
            "index_nodes_visited": self.index_nodes_visited,
        }

    def reset_stats(self):
        self.index_nodes_visited = 0

    # ------------------------------------------------------------------
    # Primary-key encoding (ISSUE-25) — fixed-width bytes per type.
    # ------------------------------------------------------------------
    def _pk_meta(self, type_name: str):
        schema = self.catalog_get_type(type_name)
        if schema is None:
            return ("int", self.INT_KEY_BYTES)
        pk_type = schema["fields"][schema["pk_order"] - 1][1]
        return (pk_type, self.INT_KEY_BYTES if pk_type == "int" else self.STR_KEY_BYTES)

    def _encode_pk(self, pk, pk_type: str) -> bytes:
        if pk_type == "int":
            return int(pk).to_bytes(self.INT_KEY_BYTES, "big", signed=False)
        return str(pk).encode("utf-8").ljust(self.STR_KEY_BYTES, b"\x00")

    def _hash_bucket_for(self, pk_bytes: bytes) -> int:
        h = int.from_bytes(hashlib.md5(pk_bytes).digest()[:4], "big")
        return h % self.hash_buckets

    # ------------------------------------------------------------------
    # Heap insert / scan (slotted page with bitmap occupancy)
    # ------------------------------------------------------------------
    def format_new_page(self) -> bytes:
        header = struct.pack(self.SLOTTED_HEADER_FMT, 0, self.page_size, 0)
        return header.ljust(self.page_size, b"\x00")

    def insert_record(self, type_name: str, record_data: bytes, primary_key=None) -> InsertResult:
        page_id, rec_offset = self._insert_into_heap(type_name, record_data)
        if primary_key is not None:
            if self.index_strategy == "hash_index":
                self._insert_into_hash(type_name, primary_key, page_id, rec_offset)
            elif self.index_strategy == "bplus_tree":
                self._insert_into_bptree(type_name, primary_key, page_id, rec_offset)
        return InsertResult(status="success", page_id=page_id, rec_offset=rec_offset)

    def _insert_into_heap(self, type_name: str, record_data: bytes):
        page_id = 0
        while True:
            buf_result = self.buffer.fetch_page(type_name, page_id)
            page_data = bytearray(buf_result.page.data)
            if all(b == 0 for b in page_data[:4]):
                page_data = bytearray(self.format_new_page())
            num_slots, free_space_end, bitmap = self._read_slot_header(page_data)
            required_space = len(record_data) + self.SLOT_ENTRY_SIZE
            slot_dir_end = self.SLOTTED_HEADER_SIZE + num_slots * self.SLOT_ENTRY_SIZE
            available_space = free_space_end - slot_dir_end
            if num_slots < self.max_records_per_page and available_space >= required_space:
                new_free_space = free_space_end - len(record_data)
                page_data[new_free_space:free_space_end] = record_data
                slot_offset = self._slot_dir_offset(num_slots)
                struct.pack_into(self.SLOT_ENTRY_FMT, page_data, slot_offset, new_free_space, len(record_data))
                self._write_slot_header(page_data, num_slots + 1, new_free_space, self._set_slot(bitmap, num_slots))
                self.log_and_put_page(type_name, page_id, bytes(page_data))
                return page_id, new_free_space
            page_id += 1

    def _read_record_from_slot(self, data_page: bytes, target_offset: int) -> bytes | None:
        """Helper for hash/B+ tree result fetches: locate slot whose
        rec_offset matches and the bitmap bit is set, return record bytes."""
        num_slots, _, bitmap = self._read_slot_header(data_page)
        for j in range(num_slots):
            if not self._slot_live(bitmap, j):
                continue
            slot_off = self._slot_dir_offset(j)
            ro, rl = struct.unpack_from(self.SLOT_ENTRY_FMT, data_page, slot_off)
            if ro == target_offset:
                return data_page[ro:ro + rl]
        return None

    # ------------------------------------------------------------------
    # Hash index (ISSUE-25 bytes key + ISSUE-12 overflow chain)
    # ------------------------------------------------------------------
    HASH_HEADER_SIZE = 6  # !H num_entries + !i next_overflow_page_id

    def _hash_entry_size(self, key_size: int) -> int:
        return key_size + 4  # key + dp(H) + offset(H)

    def _ensure_hash_initialized(self, index_file: str):
        if index_file in self._hash_initialized:
            return
        self.buffer.reserve_pages(index_file, self.hash_buckets)
        self._hash_initialized.add(index_file)

    def _hash_pack_entry(self, key_size: int, pk_bytes: bytes, dp: int, do: int) -> bytes:
        return struct.pack(f"!{key_size}sHH", pk_bytes, dp, do)

    def _hash_unpack_entry(self, key_size: int, buf: bytes, off: int):
        return struct.unpack_from(f"!{key_size}sHH", buf, off)

    def _insert_into_hash(self, type_name: str, primary_key, data_page_id: int, data_offset: int):
        index_file = f"{type_name}_hash_idx"
        pk_type, key_size = self._pk_meta(type_name)
        pk_bytes = self._encode_pk(primary_key, pk_type)
        self._ensure_hash_initialized(index_file)
        entry_size = self._hash_entry_size(key_size)
        bucket_id = self._hash_bucket_for(pk_bytes)
        page_id = bucket_id
        while True:
            buf = self.buffer.fetch_page(index_file, page_id)
            data = bytearray(buf.page.data)
            if all(b == 0 for b in data[:self.HASH_HEADER_SIZE]):
                struct.pack_into("!Hi", data, 0, 0, -1)
            num_entries, next_overflow = struct.unpack_from("!Hi", data, 0)
            entry_offset = self.HASH_HEADER_SIZE + num_entries * entry_size
            if entry_offset + entry_size <= self.page_size:
                data[entry_offset:entry_offset + entry_size] = self._hash_pack_entry(
                    key_size, pk_bytes, data_page_id, data_offset
                )
                struct.pack_into("!H", data, 0, num_entries + 1)
                self.log_and_put_page(index_file, page_id, bytes(data))
                return
            if next_overflow != -1:
                page_id = next_overflow
                continue
            # Allocate a fresh overflow page and chain it in.
            new_page_id = self.buffer.allocate_page(index_file)
            new_data = bytearray(self.page_size)
            struct.pack_into("!Hi", new_data, 0, 0, -1)
            self.log_and_put_page(index_file, new_page_id, bytes(new_data))
            struct.pack_into("!i", data, 2, new_page_id)
            self.log_and_put_page(index_file, page_id, bytes(data))
            page_id = new_page_id

    def search_hash(self, type_name: str, primary_key) -> RecordResult:
        index_file = f"{type_name}_hash_idx"
        pk_type, key_size = self._pk_meta(type_name)
        pk_bytes = self._encode_pk(primary_key, pk_type)
        entry_size = self._hash_entry_size(key_size)
        bucket_id = self._hash_bucket_for(pk_bytes)
        page_id = bucket_id
        nodes_visited = 0
        while page_id != -1:
            buf = self.buffer.fetch_page(index_file, page_id)
            data = buf.page.data
            nodes_visited += 1
            if all(b == 0 for b in data[:self.HASH_HEADER_SIZE]):
                break
            num_entries, next_overflow = struct.unpack_from("!Hi", data, 0)
            for i in range(num_entries):
                eo = self.HASH_HEADER_SIZE + i * entry_size
                ekey, dp, do = self._hash_unpack_entry(key_size, data, eo)
                if ekey == pk_bytes:
                    data_buf = self.buffer.fetch_page(type_name, dp)
                    rec = self._read_record_from_slot(data_buf.page.data, do)
                    if rec is not None:
                        self.index_nodes_visited += nodes_visited
                        return RecordResult(records=[rec], pages_accessed=1,
                                            index_nodes_visited=nodes_visited, status="success")
            page_id = next_overflow
        self.index_nodes_visited += max(nodes_visited, 1)
        return RecordResult(records=[], pages_accessed=0,
                            index_nodes_visited=max(nodes_visited, 1), status="not_found")

    def purge_from_hash(self, type_name: str, primary_key) -> bool:
        index_file = f"{type_name}_hash_idx"
        pk_type, key_size = self._pk_meta(type_name)
        pk_bytes = self._encode_pk(primary_key, pk_type)
        entry_size = self._hash_entry_size(key_size)
        bucket_id = self._hash_bucket_for(pk_bytes)
        page_id = bucket_id
        while page_id != -1:
            buf = self.buffer.fetch_page(index_file, page_id)
            data = bytearray(buf.page.data)
            if all(b == 0 for b in data[:self.HASH_HEADER_SIZE]):
                return False
            num_entries, next_overflow = struct.unpack_from("!Hi", data, 0)
            found_at = -1
            for i in range(num_entries):
                eo = self.HASH_HEADER_SIZE + i * entry_size
                ekey, _, _ = self._hash_unpack_entry(key_size, data, eo)
                if ekey == pk_bytes:
                    found_at = i
                    break
            if found_at >= 0:
                for j in range(found_at, num_entries - 1):
                    src = self.HASH_HEADER_SIZE + (j + 1) * entry_size
                    dst = self.HASH_HEADER_SIZE + j * entry_size
                    data[dst:dst + entry_size] = data[src:src + entry_size]
                last = self.HASH_HEADER_SIZE + (num_entries - 1) * entry_size
                data[last:last + entry_size] = b"\x00" * entry_size
                struct.pack_into("!H", data, 0, num_entries - 1)
                self.log_and_put_page(index_file, page_id, bytes(data))
                return True
            page_id = next_overflow
        return False

    # ------------------------------------------------------------------
    # B+ tree (ISSUE-10 recursive insert + ISSUE-25 bytes keys)
    # ------------------------------------------------------------------
    BPTREE_HEADER_SIZE = 7  # !bHi
    BPTREE_RIGHT_PTR_SIZE = 4

    def _bptree_leaf_entry_size(self, key_size: int) -> int:
        return key_size + 4

    def _bptree_internal_entry_size(self, key_size: int) -> int:
        return key_size + 4

    def _bptree_leaf_capacity(self, key_size: int) -> int:
        return (self.page_size - self.BPTREE_HEADER_SIZE) // self._bptree_leaf_entry_size(key_size)

    def _bptree_internal_capacity(self, key_size: int) -> int:
        return (self.page_size - self.BPTREE_HEADER_SIZE - self.BPTREE_RIGHT_PTR_SIZE) // self._bptree_internal_entry_size(key_size)

    def _get_bptree_root(self, index_file: str) -> int:
        buf_result = self.buffer.fetch_page(index_file, 0)
        page_data = bytearray(buf_result.page.data)
        if all(b == 0 for b in page_data[:4]):
            root_id = 1
            struct.pack_into("!i", page_data, 0, root_id)
            root_data = bytearray(self.page_size)
            struct.pack_into("!bHi", root_data, 0, 1, 0, -1)
            self.buffer.reserve_pages(index_file, 2)
            self.log_and_put_page(index_file, 0, bytes(page_data))
            self.log_and_put_page(index_file, 1, bytes(root_data))
            return root_id
        root_id, = struct.unpack_from("!i", page_data, 0)
        return root_id

    def _write_bptree_leaf(self, index_file: str, page_id: int, key_size: int,
                           entries: list, next_leaf_id: int):
        data = bytearray(self.page_size)
        struct.pack_into("!bHi", data, 0, 1, len(entries), next_leaf_id)
        eo = self.BPTREE_HEADER_SIZE
        es = self._bptree_leaf_entry_size(key_size)
        for k_bytes, dp, do in entries:
            struct.pack_into(f"!{key_size}sHH", data, eo, k_bytes, dp, do)
            eo += es
        self.log_and_put_page(index_file, page_id, bytes(data))

    def _write_bptree_internal(self, index_file: str, page_id: int, key_size: int,
                               entries: list, right_ptr: int):
        data = bytearray(self.page_size)
        struct.pack_into("!bHi", data, 0, 0, len(entries), -1)
        eo = self.BPTREE_HEADER_SIZE
        es = self._bptree_internal_entry_size(key_size)
        for k_bytes, lc in entries:
            struct.pack_into(f"!{key_size}si", data, eo, k_bytes, lc)
            eo += es
        struct.pack_into("!i", data, eo, right_ptr)
        self.log_and_put_page(index_file, page_id, bytes(data))

    def _read_bptree_leaf_entries(self, data: bytes, num_keys: int, key_size: int):
        entries = []
        es = self._bptree_leaf_entry_size(key_size)
        for i in range(num_keys):
            eo = self.BPTREE_HEADER_SIZE + i * es
            k_bytes, dp, do = struct.unpack_from(f"!{key_size}sHH", data, eo)
            entries.append((k_bytes, dp, do))
        return entries

    def _read_bptree_internal_entries(self, data: bytes, num_keys: int, key_size: int):
        entries = []
        es = self._bptree_internal_entry_size(key_size)
        for i in range(num_keys):
            eo = self.BPTREE_HEADER_SIZE + i * es
            k_bytes, lc = struct.unpack_from(f"!{key_size}si", data, eo)
            entries.append((k_bytes, lc))
        right_ptr, = struct.unpack_from("!i", data, self.BPTREE_HEADER_SIZE + num_keys * es)
        return entries, right_ptr

    def _insert_into_bptree(self, type_name: str, primary_key, data_page_id: int, data_offset: int):
        index_file = f"{type_name}_bptree_idx"
        pk_type, key_size = self._pk_meta(type_name)
        pk_bytes = self._encode_pk(primary_key, pk_type)
        root_id = self._get_bptree_root(index_file)
        result = self._bptree_insert_recursive(index_file, root_id, key_size,
                                               pk_bytes, data_page_id, data_offset)
        if result is None:
            return
        promoted_key, new_right_id = result
        new_root_id = self.buffer.allocate_page(index_file)
        self._write_bptree_internal(index_file, new_root_id, key_size,
                                    [(promoted_key, root_id)], new_right_id)
        buf0 = self.buffer.fetch_page(index_file, 0)
        page0 = bytearray(buf0.page.data)
        struct.pack_into("!i", page0, 0, new_root_id)
        self.log_and_put_page(index_file, 0, bytes(page0))

    def _bptree_insert_recursive(self, index_file: str, page_id: int, key_size: int,
                                 key_bytes: bytes, data_page_id: int, data_offset: int):
        buf = self.buffer.fetch_page(index_file, page_id)
        data = buf.page.data
        is_leaf, num_keys, next_leaf = struct.unpack_from("!bHi", data, 0)

        if is_leaf == 1:
            entries = self._read_bptree_leaf_entries(data, num_keys, key_size)
            entries.append((key_bytes, data_page_id, data_offset))
            entries.sort(key=lambda x: x[0])
            if len(entries) <= self._bptree_leaf_capacity(key_size):
                self._write_bptree_leaf(index_file, page_id, key_size, entries, next_leaf)
                return None
            mid = (len(entries) + 1) // 2
            left = entries[:mid]
            right = entries[mid:]
            new_right_id = self.buffer.allocate_page(index_file)
            self._write_bptree_leaf(index_file, new_right_id, key_size, right, next_leaf)
            self._write_bptree_leaf(index_file, page_id, key_size, left, new_right_id)
            return (right[0][0], new_right_id)

        entries, right_ptr = self._read_bptree_internal_entries(data, num_keys, key_size)
        child_id = right_ptr
        for ek, lc in entries:
            if key_bytes < ek:
                child_id = lc
                break
        result = self._bptree_insert_recursive(index_file, child_id, key_size,
                                               key_bytes, data_page_id, data_offset)
        if result is None:
            return None
        promoted_key, new_child_id = result
        if child_id == right_ptr:
            entries.append((promoted_key, child_id))
            right_ptr = new_child_id
        else:
            for i in range(len(entries)):
                if entries[i][1] == child_id:
                    entries[i] = (entries[i][0], new_child_id)
                    entries.insert(i, (promoted_key, child_id))
                    break
        if len(entries) <= self._bptree_internal_capacity(key_size):
            self._write_bptree_internal(index_file, page_id, key_size, entries, right_ptr)
            return None
        mid = len(entries) // 2
        median_key, median_left = entries[mid]
        left_entries = entries[:mid]
        right_entries = entries[mid + 1:]
        new_right_id = self.buffer.allocate_page(index_file)
        self._write_bptree_internal(index_file, page_id, key_size, left_entries, median_left)
        self._write_bptree_internal(index_file, new_right_id, key_size, right_entries, right_ptr)
        return (median_key, new_right_id)

    def search_bptree(self, type_name: str, primary_key) -> RecordResult:
        index_file = f"{type_name}_bptree_idx"
        pk_type, key_size = self._pk_meta(type_name)
        key_bytes = self._encode_pk(primary_key, pk_type)
        current_page_id = self._get_bptree_root(index_file)
        nodes_visited = 0
        while True:
            buf = self.buffer.fetch_page(index_file, current_page_id)
            data = buf.page.data
            nodes_visited += 1
            is_leaf, num_keys, _ = struct.unpack_from("!bHi", data, 0)
            if is_leaf == 1:
                entries = self._read_bptree_leaf_entries(data, num_keys, key_size)
                for ek, dp, do in entries:
                    if ek == key_bytes:
                        data_buf = self.buffer.fetch_page(type_name, dp)
                        rec = self._read_record_from_slot(data_buf.page.data, do)
                        if rec is not None:
                            self.index_nodes_visited += nodes_visited
                            return RecordResult(records=[rec], pages_accessed=1,
                                                index_nodes_visited=nodes_visited, status="success")
                self.index_nodes_visited += nodes_visited
                return RecordResult(records=[], pages_accessed=0,
                                    index_nodes_visited=nodes_visited, status="not_found")
            entries, right_ptr = self._read_bptree_internal_entries(data, num_keys, key_size)
            child_id = right_ptr
            for ek, lc in entries:
                if key_bytes < ek:
                    child_id = lc
                    break
            current_page_id = child_id

    def range_search_bptree(self, type_name: str, low: int, high: int) -> RecordResult:
        index_file = f"{type_name}_bptree_idx"
        pk_type, key_size = self._pk_meta(type_name)
        low_bytes = self._encode_pk(low, "int")
        high_bytes = self._encode_pk(high, "int")
        if key_size != self.INT_KEY_BYTES:
            return RecordResult(records=[], pages_accessed=0, index_nodes_visited=0, status="success")

        buf0 = self.buffer.fetch_page(index_file, 0)
        if all(b == 0 for b in buf0.page.data[:4]):
            self.index_nodes_visited += 1
            return RecordResult(records=[], pages_accessed=0, index_nodes_visited=1, status="success")
        root_id, = struct.unpack_from("!i", buf0.page.data, 0)

        nodes_visited = 0
        page_id = root_id
        while True:
            buf = self.buffer.fetch_page(index_file, page_id)
            data = buf.page.data
            nodes_visited += 1
            is_leaf, num_keys, _ = struct.unpack_from("!bHi", data, 0)
            if is_leaf == 1:
                break
            entries, right_ptr = self._read_bptree_internal_entries(data, num_keys, key_size)
            child_id = right_ptr
            for ek, lc in entries:
                if low_bytes < ek:
                    child_id = lc
                    break
            page_id = child_id

        records = []
        pages_accessed = 0
        while page_id != -1:
            buf = self.buffer.fetch_page(index_file, page_id)
            data = buf.page.data
            is_leaf, num_keys, next_leaf = struct.unpack_from("!bHi", data, 0)
            if is_leaf != 1:
                break
            entries = self._read_bptree_leaf_entries(data, num_keys, key_size)
            done = False
            for ek, dp, do in entries:
                if ek < low_bytes:
                    continue
                if ek > high_bytes:
                    done = True
                    break
                data_buf = self.buffer.fetch_page(type_name, dp)
                rec = self._read_record_from_slot(data_buf.page.data, do)
                if rec is not None:
                    records.append(rec)
                    pages_accessed += 1
            if done:
                break
            page_id = next_leaf

        self.index_nodes_visited += nodes_visited
        return RecordResult(records=records, pages_accessed=pages_accessed,
                            index_nodes_visited=nodes_visited, status="success")

    def purge_from_bptree(self, type_name: str, primary_key) -> bool:
        index_file = f"{type_name}_bptree_idx"
        pk_type, key_size = self._pk_meta(type_name)
        key_bytes = self._encode_pk(primary_key, pk_type)
        es_leaf = self._bptree_leaf_entry_size(key_size)

        buf0 = self.buffer.fetch_page(index_file, 0)
        if all(b == 0 for b in buf0.page.data[:4]):
            return False
        root_id, = struct.unpack_from("!i", buf0.page.data, 0)
        page_id = root_id
        while True:
            buf = self.buffer.fetch_page(index_file, page_id)
            data = buf.page.data
            is_leaf, num_keys, _ = struct.unpack_from("!bHi", data, 0)
            if is_leaf == 1:
                break
            entries, right_ptr = self._read_bptree_internal_entries(data, num_keys, key_size)
            child_id = right_ptr
            for ek, lc in entries:
                if key_bytes < ek:
                    child_id = lc
                    break
            page_id = child_id

        leaf_buf = self.buffer.fetch_page(index_file, page_id)
        leaf_data = bytearray(leaf_buf.page.data)
        _, num_keys, _ = struct.unpack_from("!bHi", leaf_data, 0)
        found_at = -1
        for i in range(num_keys):
            eo = self.BPTREE_HEADER_SIZE + i * es_leaf
            ek, _, _ = struct.unpack_from(f"!{key_size}sHH", leaf_data, eo)
            if ek == key_bytes:
                found_at = i
                break
        if found_at < 0:
            return False
        for j in range(found_at, num_keys - 1):
            src = self.BPTREE_HEADER_SIZE + (j + 1) * es_leaf
            dst = self.BPTREE_HEADER_SIZE + j * es_leaf
            leaf_data[dst:dst + es_leaf] = leaf_data[src:src + es_leaf]
        last = self.BPTREE_HEADER_SIZE + (num_keys - 1) * es_leaf
        leaf_data[last:last + es_leaf] = b"\x00" * es_leaf
        struct.pack_into("!H", leaf_data, 1, num_keys - 1)
        self.log_and_put_page(index_file, page_id, bytes(leaf_data))
        return True

    # ------------------------------------------------------------------
    # Heap helpers (slotted page with bitmap occupancy)
    # ------------------------------------------------------------------
    def iter_slot_locations(self, type_name: str):
        page_id = 0
        while True:
            buf_result = self.buffer.fetch_page(type_name, page_id)
            page_data = buf_result.page.data
            if all(b == 0 for b in page_data[:4]):
                return
            num_slots, _, bitmap = self._read_slot_header(page_data)
            for i in range(num_slots):
                if not self._slot_live(bitmap, i):
                    continue
                slot_off = self._slot_dir_offset(i)
                rec_offset, rec_len = struct.unpack_from(self.SLOT_ENTRY_FMT, page_data, slot_off)
                yield (page_id, rec_offset, page_data[rec_offset:rec_offset + rec_len])
            page_id += 1

    def tombstone_record(self, type_name: str, page_id: int, target_offset: int) -> bool:
        buf_result = self.buffer.fetch_page(type_name, page_id)
        data = bytearray(buf_result.page.data)
        num_slots, free_space_end, bitmap = self._read_slot_header(data)
        for i in range(num_slots):
            if not self._slot_live(bitmap, i):
                continue
            slot_off = self._slot_dir_offset(i)
            rec_offset, rec_len = struct.unpack_from(self.SLOT_ENTRY_FMT, data, slot_off)
            if rec_offset == target_offset:
                self._write_slot_header(data, num_slots, free_space_end, self._clear_slot(bitmap, i))
                self.log_and_put_page(type_name, page_id, bytes(data))
                return True
        return False

    def delete_at(self, type_name: str, page_id: int, rec_offset: int, primary_key) -> DeleteResult:
        """ISSUE-15: single public entry point for delete. Combines heap
        tombstone (bitmap clear) with index purge so QP doesn't have to
        orchestrate it itself."""
        if not self.tombstone_record(type_name, page_id, rec_offset):
            return DeleteResult(status="not_found")
        if self.index_strategy == "hash_index":
            self.purge_from_hash(type_name, primary_key)
        elif self.index_strategy == "bplus_tree":
            self.purge_from_bptree(type_name, primary_key)
        return DeleteResult(status="success")

    def scan_all(self, type_name: str) -> RecordResult:
        records = []
        pages_accessed = 0
        page_id = 0
        while True:
            buf_result = self.buffer.fetch_page(type_name, page_id)
            page_data = buf_result.page.data
            if all(b == 0 for b in page_data[:4]):
                break
            pages_accessed += 1
            num_slots, _, bitmap = self._read_slot_header(page_data)
            for i in range(num_slots):
                if not self._slot_live(bitmap, i):
                    continue
                slot_off = self._slot_dir_offset(i)
                rec_offset, rec_len = struct.unpack_from(self.SLOT_ENTRY_FMT, page_data, slot_off)
                records.append(page_data[rec_offset:rec_offset + rec_len])
            page_id += 1
        return RecordResult(records, pages_accessed, 0, "success")

    def log_and_put_page(self, type_name: str, page_id: int, new_data: bytes):
        """WAL Kuralları gereği veri modifikasyonundan hemen önce log atar."""
        if self.recovery_manager and self.current_xid:
            # 1. Eski veriyi (before-image) buffer'dan oku
            buf_result = self.buffer.fetch_page(type_name, page_id)
            old_data = buf_result.page.data
            
            # 2. Değişikliği logla ve LSN al
            # Basitlik ve B+ Tree karmaşasını önlemek için sayfanın tamamını logluyoruz
            lsn = self.recovery_manager.log_record(
                xid=self.current_xid,
                log_type="update",
                page_id=f"{type_name}_{page_id}",
                offset=0,
                before=old_data,
                after=new_data
            )
            # 3. BufferManager'a sayfanın yeni LSN değerini kaydet
            self.buffer.set_page_lsn(type_name, page_id, lsn)
            
        # 4. Değişikliği uygula (Mevcut işleyiş)
        self.buffer.put_page(type_name, page_id, new_data)