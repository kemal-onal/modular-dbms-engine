# query_processor/__init__.py
import struct
import time
import os


class QueryProcessor:
    """Top-level command parser + dispatcher.

    Spec syntax (Project 3 Section 7):
        create type <name> <num_fields> <pk_order> <f1_name> <f1_type> ...
        create record <type> <v1> <v2> ...
        delete record <type> <pk>
        search record <type> <pk>
        range_search <type> <field> <low> <high>
        explain <any DML command>
        stats
        stats reset

    Schemas are persisted in the system catalog (`_catalog_.bin`, ISSUE-04),
    read/written via the buffer manager.
    """

    INT_WIDTH = 4
    STR_WIDTH = 20

    def __init__(self, config: dict, file_idx, buffer, disk):
        self.config = config
        self.file_idx = file_idx
        self.buffer = buffer
        self.disk = disk

        # Cumulative since last `stats reset` (Spec Section 9.3).
        self.records_scanned = 0
        self.records_returned = 0

        # All output files live next to archive.py.
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.output_path = os.path.join(root_dir, "output.txt")
        self.log_path = os.path.join(root_dir, "log.csv")
        self.stats_path = os.path.join(root_dir, "stats_output.txt")
        self.data_dir = root_dir

        # WAL ve Transaction Takibi İçin Eklenecekler
        self.recovery_manager = None
        self.active_tx_names = set()  # Açık olan işlemleri (XID) takip eder

        # output.txt: truncate each run (spec doesn't mandate; matches sample usage).
        open(self.output_path, "w").close()
        # log.csv: append-only persistent per Spec Section 15. Create if missing.
        if not os.path.exists(self.log_path):
            open(self.log_path, "w").close()

        
    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------
    def _log(self, op_string: str, status: str):
        ts = int(time.time())
        with open(self.log_path, "a") as f:
            f.write(f"{ts},{op_string},{status}\n")

    def _emit(self, line: str):
        with open(self.output_path, "a") as f:
            f.write(line + "\n")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def process(self, line: str):
        raw = line.strip()
        if not raw:
            return
        tokens = raw.split()
        head = tokens[0].lower()

        try:
            if head == "explain":
                status = self._handle_explain(tokens[1:])
                self._log(raw, status)
                return

            if head == "stats":
                if len(tokens) == 2 and tokens[1].lower() == "reset":
                    self._handle_stats_reset()
                    self._log(raw, "success")
                    return
                if len(tokens) == 1:
                    self._handle_stats_snapshot()
                    self._log(raw, "success")
                    return
                self._log(raw, "failure")
                return

            status = self._dispatch(tokens, suppress_output=False)
            self._log(raw, status)
        except Exception:
            # Spec: "The system must not crash". Any unhandled error => failure.
            self._log(raw, "failure")

    def _dispatch(self, tokens, suppress_output: bool, is_inside_tx: bool = False) -> str:
        if not tokens:
            return "failure"
        head = tokens[0].lower()

        if head == "tx_begin" and len(tokens) == 2:
            self.active_tx_names.add(tokens[1])
            return "success"
            
        if head == "tx_commit" and len(tokens) == 2:
            tx_name = tokens[1]
            if tx_name in self.active_tx_names:
                if self.recovery_manager:
                    self.recovery_manager.commit(tx_name)
                self.active_tx_names.remove(tx_name)
                return "success"
            return "failure"
            
        if head == "tx_op" and len(tokens) >= 3:
            tx_name = tokens[1]
            if tx_name not in self.active_tx_names:
                return "failure"

            inner_tokens = tokens[2:]

            # Undo komutunu (mantiksal ters islem) op'tan ONCE hesapla; delete
            # icin kaydin silinmeden onceki degerleri gerekir.
            undo_cmd = self._compute_undo(inner_tokens)

            # XID'i alt katmana ilet ki fiziksel update kayitlari bu tx altinda
            # loglansin (FileIndexManager.log_and_put_page bunu kullanir).
            if self.recovery_manager:
                self.file_idx.current_xid = tx_name
            status = self._dispatch(inner_tokens, suppress_output, is_inside_tx=True)
            if self.recovery_manager:
                self.file_idx.current_xid = None
                if status == "success":
                    self.recovery_manager.log_op(tx_name, " ".join(inner_tokens), undo_cmd)
                    self.recovery_manager.tick()
            return status

        if head == "crash":
            if self.recovery_manager:
                self.recovery_manager.crash()
            else:
                import os
                os._exit(1)
            return "success"

        if not is_inside_tx and head in ["create", "delete", "search", "range_search"]:
            return "failure"

        if head == "create" and len(tokens) >= 2:
            sub = tokens[1].lower()
            if sub == "type":
                return self._handle_create_type(tokens[2:])
            if sub == "record":
                return self._handle_create_record(tokens[2:])
            return "failure"
            
        if head == "delete" and len(tokens) >= 2 and tokens[1].lower() == "record":
            return self._handle_delete_record(tokens[2:])
            
        if head == "search" and len(tokens) >= 2 and tokens[1].lower() == "record":
            return self._handle_search_record(tokens[2:], suppress_output)
            
        if head == "range_search":
            return self._handle_range_search(tokens[1:], suppress_output)
            
        return "failure"

    # ------------------------------------------------------------------
    # create type
    # ------------------------------------------------------------------
    def _handle_create_type(self, args) -> str:
        if len(args) < 3:
            return "failure"
        type_name = args[0]
        try:
            num_fields = int(args[1])
            pk_order = int(args[2])
        except ValueError:
            return "failure"

        field_tokens = args[3:]
        if len(field_tokens) != 2 * num_fields:
            return "failure"
        if not (1 <= pk_order <= num_fields):
            return "failure"

        fields = []
        for i in range(0, len(field_tokens), 2):
            fname = field_tokens[i]
            ftype = field_tokens[i + 1].lower()
            if ftype not in ("int", "str"):
                return "failure"
            fields.append((fname, ftype))

        ok = self.file_idx.catalog_create_type(type_name, num_fields, pk_order, fields)
        return "success" if ok else "failure"  # duplicate type or catalog overflow

    # ------------------------------------------------------------------
    # create record
    # ------------------------------------------------------------------
    def _handle_create_record(self, args) -> str:
        if len(args) < 2:
            return "failure"
        type_name = args[0]
        schema = self.file_idx.catalog_get_type(type_name)
        if schema is None:
            return "failure"
        values = args[1:]
        if len(values) != schema["num_fields"]:
            return "failure"

        try:
            record_data = self._pack_record(schema, values)
        except (ValueError, struct.error):
            return "failure"

        pk_raw = values[schema["pk_order"] - 1]
        pk_type = schema["fields"][schema["pk_order"] - 1][1]
        pk_value = self._parse_pk(pk_raw, pk_type)
        if pk_value is None:
            return "failure"

        # Duplicate-PK check (Spec Section 7.4). Uses the active index when
        # available; falls back to a heap scan for str PKs or heap_scan strategy.
        if self._search(type_name, schema, pk_value) is not None:
            return "failure"

        self.file_idx.insert_record(type_name, record_data, primary_key=pk_value)
        return "success"

    def _pack_record(self, schema, values) -> bytes:
        fmt = "!"
        packed = []
        for val, (_, ftype) in zip(values, schema["fields"]):
            if ftype == "int":
                fmt += "i"
                packed.append(int(val))
            else:
                fmt += f"{self.STR_WIDTH}s"
                packed.append(val.encode("utf-8").ljust(self.STR_WIDTH, b"\x00"))
        return struct.pack(fmt, *packed)

    def _unpack_record(self, schema, rec_bytes) -> list:
        fmt = "!"
        for _, ftype in schema["fields"]:
            fmt += "i" if ftype == "int" else f"{self.STR_WIDTH}s"
        unpacked = struct.unpack(fmt, rec_bytes)
        out = []
        for val in unpacked:
            if isinstance(val, bytes):
                out.append(val.decode("utf-8").rstrip("\x00"))
            else:
                out.append(str(val))
        return out

    def _parse_pk(self, raw, pk_type):
        if pk_type == "int":
            try:
                return int(raw)
            except ValueError:
                return None
        return str(raw)

    # ------------------------------------------------------------------
    # Undo komutu uretimi (Recovery Section 5.3) - loser islemleri mantiksal
    # olarak geri almak icin ters komut.
    # ------------------------------------------------------------------
    def _compute_undo(self, inner_tokens):
        if self.recovery_manager is None or len(inner_tokens) < 2:
            return None
        head = inner_tokens[0].lower()
        sub = inner_tokens[1].lower()
        try:
            if head == "create" and sub == "type" and len(inner_tokens) >= 3:
                return f"__drop_type__ {inner_tokens[2]}"
            if head == "create" and sub == "record" and len(inner_tokens) >= 3:
                type_name = inner_tokens[2]
                schema = self.file_idx.catalog_get_type(type_name)
                if schema is None:
                    return None
                values = inner_tokens[3:]
                if len(values) != schema["num_fields"]:
                    return None
                pk = values[schema["pk_order"] - 1]
                return f"delete record {type_name} {pk}"
            if head == "delete" and sub == "record" and len(inner_tokens) == 4:
                type_name, pk_raw = inner_tokens[2], inner_tokens[3]
                schema = self.file_idx.catalog_get_type(type_name)
                if schema is None:
                    return None
                pk_type = schema["fields"][schema["pk_order"] - 1][1]
                pk = self._parse_pk(pk_raw, pk_type)
                if pk is None:
                    return None
                found = self._scan_heap_for_pk(type_name, schema, pk)
                if found is None:
                    return None
                values = self._unpack_record(schema, found[2])
                return f"create record {type_name} " + " ".join(values)
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # search record
    # ------------------------------------------------------------------
    def _handle_search_record(self, args, suppress_output: bool) -> str:
        if len(args) != 2:
            return "failure"
        type_name, pk_raw = args
        schema = self.file_idx.catalog_get_type(type_name)
        if schema is None:
            return "failure"
        pk_type = schema["fields"][schema["pk_order"] - 1][1]
        pk = self._parse_pk(pk_raw, pk_type)
        if pk is None:
            return "failure"

        record = self._search(type_name, schema, pk)
        if record is None:
            return "failure"
        if not suppress_output:
            self._emit(" ".join(record))
            self.records_returned += 1
        return "success"

    def _search(self, type_name, schema, pk):
        """Return the matched record as a list of strings, or None."""
        strategy = self.file_idx.index_strategy
        if strategy == "hash_index":
            rr = self.file_idx.search_hash(type_name, pk)
            self.records_scanned += len(rr.records) if rr.records else 1
        elif strategy == "bplus_tree":
            rr = self.file_idx.search_bptree(type_name, pk)
            self.records_scanned += len(rr.records) if rr.records else 1
        else:
            rec_bytes = self._heap_lookup_by_pk(type_name, schema, pk)
            return self._unpack_record(schema, rec_bytes) if rec_bytes is not None else None

        if rr.status != "success" or not rr.records:
            return None
        return self._unpack_record(schema, rr.records[0])

    def _heap_lookup_by_pk(self, type_name, schema, pk):
        """Linear scan of the heap, matching the PK field. Returns raw bytes or None."""
        found = self._scan_heap_for_pk(type_name, schema, pk)
        return found[2] if found is not None else None

    def _find_heap_location(self, type_name, schema, pk):
        """Scan the heap and return (page_id, rec_offset) for the matching PK, or None."""
        found = self._scan_heap_for_pk(type_name, schema, pk)
        return (found[0], found[1]) if found is not None else None

    def _scan_heap_for_pk(self, type_name, schema, pk):
        """Single shared heap scan. Returns (page_id, rec_offset, rec_bytes) or None.
        Uses iter_slot_locations so the slotted-page format stays owned by
        FileIndexManager (ISSUE-13)."""
        pk_idx = schema["pk_order"] - 1
        pk_type = schema["fields"][pk_idx][1]
        for page_id, rec_offset, rec_bytes in self.file_idx.iter_slot_locations(type_name):
            self.records_scanned += 1
            vals = self._unpack_record(schema, rec_bytes)
            if pk_type == "int":
                try:
                    if int(vals[pk_idx]) == int(pk):
                        return (page_id, rec_offset, rec_bytes)
                except ValueError:
                    continue
            else:
                if vals[pk_idx] == str(pk):
                    return (page_id, rec_offset, rec_bytes)
        return None

    # ------------------------------------------------------------------
    # delete record  (ISSUE-05)
    # ------------------------------------------------------------------
    def _handle_delete_record(self, args) -> str:
        if len(args) != 2:
            return "failure"
        type_name, pk_raw = args
        schema = self.file_idx.catalog_get_type(type_name)
        if schema is None:
            return "failure"
        pk_type = schema["fields"][schema["pk_order"] - 1][1]
        pk = self._parse_pk(pk_raw, pk_type)
        if pk is None:
            return "failure"

        loc = self._find_heap_location(type_name, schema, pk)
        if loc is None:
            return "failure"  # spec Section 7.4

        page_id, rec_offset = loc
        result = self.file_idx.delete_at(type_name, page_id, rec_offset, pk)
        return "success" if result.status == "success" else "failure"

    # ------------------------------------------------------------------
    # range_search  (heap-scan baseline + B+ tree leaf chain for PK ranges)
    # ------------------------------------------------------------------
    def _handle_range_search(self, args, suppress_output: bool) -> str:
        if len(args) != 4:
            return "failure"
        type_name, field_name, low_raw, high_raw = args
        schema = self.file_idx.catalog_get_type(type_name)
        if schema is None:
            return "failure"
            
        field_idx = None
        field_type = None
        for i, (fname, ftype) in enumerate(schema["fields"]):
            if fname == field_name:
                field_idx = i
                field_type = ftype
                break
                
        if field_idx is None or field_type != "int":
            return "failure"  
            
        try:
            low = int(low_raw)
            high = int(high_raw)
        except ValueError:
            return "failure"

        pk_idx = schema["pk_order"] - 1
        pk_type = schema["fields"][pk_idx][1]
        matching_records = []

        # ISSUE-06: B+ tree
        if (field_idx == pk_idx and field_type == "int" and self.file_idx.index_strategy == "bplus_tree"):
            rr = self.file_idx.range_search_bptree(type_name, low, high)
            self.records_scanned += len(rr.records)
            for rec_bytes in rr.records:
                matching_records.append(self._unpack_record(schema, rec_bytes))
        else:
            # Default: heap scan
            for _, _, rec_bytes in self.file_idx.iter_slot_locations(type_name):
                self.records_scanned += 1
                vals = self._unpack_record(schema, rec_bytes)
                v = int(vals[field_idx])
                if low <= v <= high:
                    matching_records.append(vals)

        # ŞARTNAME: Belirtilen alana göre artan, eşitlikte PK'ye göre artan sıralama
        def sort_key(vals):
            primary_sort = int(vals[field_idx])
            if pk_type == "int":
                secondary_sort = int(vals[pk_idx])
            else:
                secondary_sort = str(vals[pk_idx])
            return (primary_sort, secondary_sort)

        matching_records.sort(key=sort_key)

        # Sıralanmış sonuçları dosyaya yazdır
        for vals in matching_records:
            self.records_returned += 1
            if not suppress_output:
                self._emit(" ".join(vals))
                
        return "success"
    # ------------------------------------------------------------------
    # explain  (ISSUE-07)
    # ------------------------------------------------------------------
    def _handle_explain(self, inner_tokens) -> str:
        if not inner_tokens:
            return "failure"
        inner_query = " ".join(inner_tokens)
        strategy = self.file_idx.index_strategy
        type_name = inner_tokens[2] if len(inner_tokens) >= 3 else ""
        est_io = self._estimate_io(inner_tokens, type_name)

        # PLAN block.
        self._emit("--- PLAN ---")
        self._emit(f"Query:           {inner_query}")
        self._emit(f"Strategy:        {strategy}")
        self._emit(f"Estimated I/O:   {est_io}")

        # Snapshot stats BEFORE the inner op runs.
        before = self._snapshot_stats()

        # RESULT block — header first, then dispatch writes record lines underneath.
        self._emit("--- RESULT ---")
        status = self._dispatch(inner_tokens, suppress_output=False)

        # STATS block — delta sayaçlar.
        after = self._snapshot_stats()
        delta = {k: after[k] - before[k] for k in before}
        self._emit("--- STATS ---")
        self._emit(f"Actual I/O:      {delta['disk_reads']} reads, {delta['disk_writes']} writes")
        self._emit(f"Buffer Hits:     {delta['buffer_hits']}")
        self._emit(f"Buffer Misses:   {delta['buffer_misses']}")
        self._emit(f"Pages Scanned:   {delta['buffer_requests']}")
        return status

    def _estimate_io(self, inner_tokens, type_name) -> int:
        if not inner_tokens:
            return 0
        head = inner_tokens[0].lower()
        sub = inner_tokens[1].lower() if len(inner_tokens) > 1 else ""
        strategy = self.file_idx.index_strategy

        if head == "create" and sub == "type":
            return 0
        if head == "create" and sub == "record":
            if strategy == "heap_scan":
                return 1
            if strategy == "hash_index":
                return 2
            return 3
        if head == "search" and sub == "record":
            if strategy == "heap_scan":
                return self._heap_page_count(type_name)
            if strategy == "hash_index":
                return 2
            return 3
        if head == "delete" and sub == "record":
            base = self._heap_page_count(type_name)
            return base + (0 if strategy == "heap_scan" else 1)
        if head == "range_search":
            return self._heap_page_count(type_name)
        return 0

    def _heap_page_count(self, type_name) -> int:
        path = os.path.join(self.data_dir, f"{type_name}.bin")
        if not os.path.exists(path):
            return 1
        size = os.path.getsize(path)
        page_size = self.config.get("page_size", 4096)
        return max(1, size // page_size)

    # ------------------------------------------------------------------
    # stats  (ISSUE-08)
    # ------------------------------------------------------------------
    def _snapshot_stats(self) -> dict:
        ds = self.disk.get_stats()
        bs = self.buffer.get_stats()
        fs = self.file_idx.get_stats()
        return {
            "disk_reads": ds["reads"],
            "disk_writes": ds["writes"],
            "buffer_requests": bs["requests"],
            "buffer_hits": bs["hits"],
            "buffer_misses": bs["misses"],
            "evictions": bs["evictions"],
            "dirty_writebacks": bs["dirty_writebacks"],
            "index_nodes_visited": fs["index_nodes_visited"],
            "records_scanned": self.records_scanned,
            "records_returned": self.records_returned,
        }

    def _handle_stats_snapshot(self):
        ds = self.disk.get_stats()
        bs = self.buffer.get_stats()
        fs = self.file_idx.get_stats()
        hit_rate = (bs["hits"] / bs["requests"] * 100.0) if bs["requests"] else 0.0
        lines = [
            "=== STATISTICS ===",
            f"Disk I/O:        {ds['reads']} reads, {ds['writes']} writes",
            f"Buffer Pool:     {bs['requests']} requests, {bs['hits']} hits, {bs['misses']} misses ({hit_rate:.1f}% hit rate)",
            f"Evictions:       {bs['evictions']} ({bs['dirty_writebacks']} dirty writebacks)",
            f"Index:           {fs['index_strategy']}, {fs['index_nodes_visited']} nodes visited",
            f"Records:         {self.records_scanned} scanned, {self.records_returned} returned",
        ]
        with open(self.stats_path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def _handle_stats_reset(self):
        self.disk.reset_stats()
        self.buffer.reset_stats()
        self.file_idx.reset_stats()
        self.records_scanned = 0
        self.records_returned = 0
