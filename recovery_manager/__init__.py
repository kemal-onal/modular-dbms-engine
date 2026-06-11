import os
import ast

# Recovery Manager (Spec Section 4) - WAL + sadelestirilmis ARIES.
#
# Tasarim notu (Spec Section 3 + 5):
#   - REDO fizikseldir: her update kaydi sayfanin tam after-image'ini tutar,
#     repeat-history mantigiyla LSN sirasinda yeniden yazilir. Ayni sayfaya
#     birden cok islem dokunsa bile son yazan kazanir, yani cokme ani state'i
#     dogru kurulur ve tekrar uygulamak idempotenttir.
#   - UNDO mantiksaldir: loser islemler kayit bazinda ters komutla geri alinir
#     (create -> delete, delete -> create). Boylece paylasilan sayfa header'i
#     bozulmaz (tam-sayfa before-image geri yazmanin yan etkisi olmaz).
#   - Log dosyasi (wal.log) Recovery Manager'a aittir; buffer pool'a girmez.


class RecoveryManager:
    def __init__(self, config: dict, disk):
        self.config = config
        self.disk = disk
        self.log_file_path = "wal.log"
        self.master_path = "master.log"

        self.log_buffer_size = config.get("log_buffer_size", 8)
        self.checkpoint_interval = config.get("checkpoint_interval", 50)

        # In-memory ARIES yapilari (Spec Section 4.1)
        self.tx_table = {}      # xid -> {"status": str, "last_lsn": int}
        self.dirty_page_table = {}  # page_key (str) -> recLSN
        self.last_lsn = {}      # xid -> son kaydin LSN'i (prev_lsn zinciri icin)

        self.log_buffer = []
        self.flushed_lsn = 0
        self.op_count = 0
        self.in_recovery = False

        # LSN restart'ta sifirlanmaz; diskteki log'dan ve master'dan devam eder.
        self.current_lsn = self._recover_lsn_counter()
        self.flushed_lsn = self.current_lsn

    # ------------------------------------------------------------------
    # Log dosyasi okuma / yazma (buffer pool bypass)
    # ------------------------------------------------------------------
    def _read_all_records(self):
        records = []
        if not os.path.exists(self.log_file_path):
            return records
        with open(self.log_file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(ast.literal_eval(line))
                except (ValueError, SyntaxError):
                    pass
        return records

    def _recover_lsn_counter(self) -> int:
        max_lsn = 0
        for rec in self._read_all_records():
            if rec.get("lsn", 0) > max_lsn:
                max_lsn = rec["lsn"]
        if os.path.exists(self.master_path):
            try:
                with open(self.master_path, "r") as f:
                    _, next_lsn = f.read().strip().split(",")
                    max_lsn = max(max_lsn, int(next_lsn))
            except (ValueError, OSError):
                pass
        return max_lsn

    def _flush_buffer(self):
        if not self.log_buffer:
            return
        with open(self.log_file_path, "a") as f:
            for rec in self.log_buffer:
                f.write(repr(rec) + "\n")
                if rec["lsn"] > self.flushed_lsn:
                    self.flushed_lsn = rec["lsn"]
            f.flush()
            os.fsync(f.fileno())
        self.log_buffer.clear()

    def _append(self, record: dict):
        self.log_buffer.append(record)
        if len(self.log_buffer) >= self.log_buffer_size:
            self._flush_buffer()

    def _next_lsn(self) -> int:
        self.current_lsn += 1
        return self.current_lsn

    # ------------------------------------------------------------------
    # Ust katmanlarin kullandigi loglama kontrati
    # ------------------------------------------------------------------
    def log_record(self, xid, log_type, page_id=None, offset=0, before=None, after=None):
        """FileIndexManager her sayfa degisiminden once cagirir (fiziksel update)."""
        lsn = self._next_lsn()
        record = {
            "lsn": lsn,
            "prev_lsn": self.last_lsn.get(xid),
            "xid": xid,
            "type": log_type,
            "page_id": page_id,
            "offset": offset,
            "before": before,
            "after": after,
        }
        self.last_lsn[xid] = lsn
        self.tx_table[xid] = {"status": "active", "last_lsn": lsn}
        if page_id is not None and page_id not in self.dirty_page_table:
            self.dirty_page_table[page_id] = lsn
        self._append(record)
        return lsn

    def log_op(self, xid, command, undo):
        """QueryProcessor her tx_op icin mantiksal komutu ve tersini loglar (undo icin)."""
        lsn = self._next_lsn()
        record = {
            "lsn": lsn,
            "prev_lsn": self.last_lsn.get(xid),
            "xid": xid,
            "type": "op",
            "command": command,
            "undo": undo,
        }
        self.last_lsn[xid] = lsn
        self.tx_table[xid] = {"status": "active", "last_lsn": lsn}
        self._append(record)
        return lsn

    def commit(self, xid):
        """WAL #2 (Durability): commit kaydini yaz, fsync'le, sonra end yaz."""
        lsn = self._next_lsn()
        self._append({
            "lsn": lsn, "prev_lsn": self.last_lsn.get(xid),
            "xid": xid, "type": "commit",
        })
        self.last_lsn[xid] = lsn
        if xid in self.tx_table:
            self.tx_table[xid]["status"] = "committed"
            self.tx_table[xid]["last_lsn"] = lsn
        self._flush_buffer()  # commit kaydi dahil her sey diske + fsync

        end_lsn = self._next_lsn()
        self._append({
            "lsn": end_lsn, "prev_lsn": lsn, "xid": xid, "type": "end",
        })
        self._flush_buffer()
        self.tx_table.pop(xid, None)
        self.last_lsn.pop(xid, None)

    def flush_log_up_to(self, lsn: int):
        """WAL #1 (Atomicity): BufferManager kirli sayfayi yazmadan once cagirir."""
        if self.flushed_lsn < lsn:
            self._flush_buffer()

    def page_flushed(self, type_name, page_id):
        """Sayfa diske temiz yazilinca DPT'den dusur."""
        self.dirty_page_table.pop(f"{type_name}_{page_id}", None)

    def crash(self):
        # Spec Section 7: ani guc kesintisi. Hicbir flush/cleanup calismaz.
        os._exit(1)

    # ------------------------------------------------------------------
    # Fuzzy checkpoint (Spec Section 5.1)
    # ------------------------------------------------------------------
    def tick(self):
        """QueryProcessor her mantiksal islemden sonra cagirir."""
        self.op_count += 1
        if self.checkpoint_interval > 0 and self.op_count % self.checkpoint_interval == 0:
            self.checkpoint()

    def checkpoint(self):
        bc_lsn = self._next_lsn()
        self._append({"lsn": bc_lsn, "type": "begin_checkpoint"})
        ec_lsn = self._next_lsn()
        self._append({
            "lsn": ec_lsn,
            "type": "end_checkpoint",
            "tx_table": {x: [v["status"], v["last_lsn"]] for x, v in self.tx_table.items()},
            "dirty_page_table": dict(self.dirty_page_table),
        })
        self._flush_buffer()
        self._write_master(bc_lsn)

    def _write_master(self, bc_lsn: int):
        with open(self.master_path, "w") as f:
            f.write(f"{bc_lsn},{self.current_lsn}")
            f.flush()
            os.fsync(f.fileno())

    def _read_master(self):
        if not os.path.exists(self.master_path):
            return None
        try:
            with open(self.master_path, "r") as f:
                bc_lsn, _ = f.read().strip().split(",")
                return int(bc_lsn)
        except (ValueError, OSError):
            return None

    # ------------------------------------------------------------------
    # Uc fazli recovery (Spec Section 5) - startup'ta input islenmeden once
    # ------------------------------------------------------------------
    def recover(self, engine):
        records = self._read_all_records()
        if not records:
            return

        self.in_recovery = True
        tx_table, dpt, losers, committed = self._analysis(records)
        self._redo(records, dpt)
        self._undo(records, losers, engine)

        # Loser'lar geri alindi; commit edip end yazmamis winner'lara end yaz.
        for xid in committed:
            self._append({
                "lsn": self._next_lsn(), "prev_lsn": self.last_lsn.get(xid),
                "xid": xid, "type": "end",
            })

        # Undo yazimlarini diske kalicilastir, sonra temiz bir checkpoint at.
        engine.buffer.flush()
        self._flush_buffer()
        self.tx_table.clear()
        self.dirty_page_table.clear()
        self.last_lsn.clear()
        self.checkpoint()
        self.in_recovery = False

    def _analysis(self, records):
        """Spec Section 5.1: checkpoint'ten ileri tarayip TT ve DPT'yi kur."""
        tx_table = {}   # xid -> status ("active"/"committed")
        last = {}       # xid -> last_lsn
        dpt = {}        # page_key -> recLSN

        bc_lsn = self._read_master()
        start_lsn = 0
        if bc_lsn is not None:
            for rec in records:
                if rec.get("type") == "end_checkpoint" and rec["lsn"] > bc_lsn:
                    for x, (status, ll) in rec.get("tx_table", {}).items():
                        tx_table[x] = status
                        last[x] = ll
                    dpt.update(rec.get("dirty_page_table", {}))
                    start_lsn = bc_lsn
                    break

        for rec in records:
            if rec["lsn"] <= start_lsn:
                continue
            rtype = rec.get("type")
            xid = rec.get("xid")
            if rtype == "update":
                tx_table[xid] = "active"
                last[xid] = rec["lsn"]
                pid = rec.get("page_id")
                if pid is not None and pid not in dpt:
                    dpt[pid] = rec["lsn"]
            elif rtype == "op":
                tx_table[xid] = tx_table.get(xid, "active")
                last[xid] = rec["lsn"]
            elif rtype == "commit":
                tx_table[xid] = "committed"
                last[xid] = rec["lsn"]
            elif rtype == "end":
                tx_table.pop(xid, None)
                last.pop(xid, None)

        losers = [x for x, s in tx_table.items() if s != "committed"]
        committed = [x for x, s in tx_table.items() if s == "committed"]
        self.last_lsn = dict(last)
        return tx_table, dpt, losers, committed

    def _redo(self, records, dpt):
        """Spec Section 5.2: repeat history. Update'leri LSN sirasinda yeniden yaz."""
        if not dpt:
            return
        min_rec_lsn = min(dpt.values())
        updates = sorted(
            (r for r in records if r.get("type") == "update"),
            key=lambda r: r["lsn"],
        )
        for rec in updates:
            lsn = rec["lsn"]
            pid = rec.get("page_id")
            if lsn < min_rec_lsn:
                continue
            if pid not in dpt or lsn < dpt[pid]:
                continue
            type_name, page_id = self._split_page_id(pid)
            if type_name is None:
                continue
            self.disk.write_page(type_name, page_id, rec["after"])

    def _undo(self, records, losers, engine):
        """Spec Section 5.3: loser'lari mantiksal ters komutla geri al."""
        if not losers:
            return
        loser_set = set(losers)
        loser_ops = sorted(
            (r for r in records if r.get("type") == "op" and r.get("xid") in loser_set),
            key=lambda r: r["lsn"],
            reverse=True,
        )
        engine.file_idx.current_xid = None  # undo yazimlari yeni WAL kaydi uretmesin
        for rec in loser_ops:
            undo_cmd = rec.get("undo")
            if not undo_cmd:
                continue
            self._apply_undo(undo_cmd, engine)

        for xid in losers:
            self._append({
                "lsn": self._next_lsn(), "prev_lsn": self.last_lsn.get(xid),
                "xid": xid, "type": "end",
            })

    def _apply_undo(self, undo_cmd: str, engine):
        tokens = undo_cmd.split()
        if not tokens:
            return
        if tokens[0] == "__drop_type__" and len(tokens) == 2:
            engine.file_idx.catalog_drop_type(tokens[1])
            return
        try:
            engine._dispatch(tokens, suppress_output=True, is_inside_tx=True)
        except Exception:
            pass

    @staticmethod
    def _split_page_id(pid: str):
        if not isinstance(pid, str) or "_" not in pid:
            return None, None
        type_name, page_str = pid.rsplit("_", 1)
        try:
            return type_name, int(page_str)
        except ValueError:
            return None, None
