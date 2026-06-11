# buffer_manager/__init__.py
from common.results import BufferResult, PageResult

class BufferManager:
    def __init__(self, config: dict, disk):
        self.disk = disk
        self.pool_size = config.get("buffer_pool_size", 16)
        self.policy = config.get("replacement_policy", "LRU")
        
        self.frames = {}          # (type_name, page_id) -> sayfa verisi (bytes)
        self.dirty_pages = set()  # Değiştirilmiş sayfaları takip eder
        self.access_history = []  # LRU/MRU için erişim sırası
        
        # İstatistikler
        self.requests = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.dirty_writebacks = 0

        # WAL
        self.recovery_manager = None
        self.page_lsns = {}  # (type_name, page_id) -> int (LSN)

    def set_page_lsn(self, type_name: str, page_id: int, lsn: int):
        """FileIndexManager bir sayfayı değiştirdiğinde log'dan dönen LSN'i buraya kaydeder."""
        self.page_lsns[(type_name, page_id)] = lsn

    def fetch_page(self, type_name: str, page_id: int) -> BufferResult:
        """Sayfayı buffer'dan veya diskten getirir."""
        self.requests += 1
        page_key = (type_name, page_id)
        
        # Cache Hit (Sayfa bellekte var)
        if page_key in self.frames:
            self.hits += 1
            self._update_access_history(page_key)
            return BufferResult(
                page=PageResult(data=self.frames[page_key], io_performed=False),
                cache_hit=True,
                evicted_page_id=None,
                dirty_writeback=False
            )
            
        # Cache Miss (Sayfa bellekte yok, diskten okunacak)
        self.misses += 1
        evicted_id = None
        dirty_wb = False
        
        # Buffer doluysa bir sayfayı çıkar (Eviction)
        if len(self.frames) >= self.pool_size:
            evicted_key = self._evict_page()
            evicted_id = evicted_key[1]
            if evicted_key in self.dirty_pages:
                # WAL Kuralı #1 (Atomicity): Sayfa diske yazılmadan önce loglar diske zorlanmalı
                if self.recovery_manager:
                    lsn = self.page_lsns.get(evicted_key, 0)
                    self.recovery_manager.flush_log_up_to(lsn)

                dirty_wb = True
                self.dirty_writebacks += 1
                self.disk.write_page(evicted_key[0], evicted_key[1], self.frames[evicted_key])
                self.dirty_pages.remove(evicted_key)
                
                # Recovery Manager'a bildir ve LSN kaydını temizle
                if self.recovery_manager:
                    self.recovery_manager.page_flushed(evicted_key[0], evicted_key[1])
                if evicted_key in self.page_lsns:
                    del self.page_lsns[evicted_key]

            del self.frames[evicted_key]
            self.evictions += 1

        # Diskten sayfayı oku ve Buffer'a ekle
        disk_result = self.disk.read_page(type_name, page_id)
        self.frames[page_key] = disk_result.data
        self.access_history.append(page_key)
        
        return BufferResult(
            page=disk_result,
            cache_hit=False,
            evicted_page_id=evicted_id,
            dirty_writeback=dirty_wb
        )

    def _update_access_history(self, page_key):
        """LRU ve MRU için erişim geçmişini günceller."""
        if page_key in self.access_history:
            self.access_history.remove(page_key)
        self.access_history.append(page_key)

    def _evict_page(self):
        """LRU veya MRU kuralına göre kurban sayfayı seçer."""
        if self.policy == "LRU":
            # En az yakın zamanda kullanılan (listenin başındaki)
            page_to_evict = self.access_history.pop(0)
        else: # MRU
            # En son kullanılan (listenin sonundaki)
            page_to_evict = self.access_history.pop(-1)
        return page_to_evict

    def flush(self):
        """Kapanışta tüm kirli (dirty) sayfaları diske yazar."""
        for page_key in list(self.dirty_pages):
            # WAL Kuralı #1
            if self.recovery_manager:
                lsn = self.page_lsns.get(page_key, 0)
                self.recovery_manager.flush_log_up_to(lsn)

            self.disk.write_page(page_key[0], page_key[1], self.frames[page_key])
            self.dirty_pages.remove(page_key)
            self.dirty_writebacks += 1

            if self.recovery_manager:
                self.recovery_manager.page_flushed(page_key[0], page_key[1])
            if page_key in self.page_lsns:
                del self.page_lsns[page_key]

    # ------------------------------------------------------------------
    # ISSUE-13 — public API for upper layers to mutate pages without
    # reaching past us into DiskSpaceManager or our private state.
    # ------------------------------------------------------------------
    def put_page(self, type_name: str, page_id: int, new_data: bytes) -> None:
        """Write new page contents into the pool and mark the frame dirty.

        On a pool miss this evicts (and writes back) using the active
        replacement policy — no disk read for the new page (the caller is
        about to overwrite it anyway).
        """
        page_key = (type_name, page_id)
        if page_key in self.frames:
            self.frames[page_key] = bytes(new_data)
            self.dirty_pages.add(page_key)
            self._update_access_history(page_key)
            return
        if len(self.frames) >= self.pool_size:
            evicted_key = self._evict_page()
            if evicted_key in self.dirty_pages:
                # WAL Kuralı #1
                if self.recovery_manager:
                    lsn = self.page_lsns.get(evicted_key, 0)
                    self.recovery_manager.flush_log_up_to(lsn)

                self.dirty_writebacks += 1
                self.disk.write_page(evicted_key[0], evicted_key[1], self.frames[evicted_key])
                self.dirty_pages.remove(evicted_key)

                if self.recovery_manager:
                    self.recovery_manager.page_flushed(evicted_key[0], evicted_key[1])
                if evicted_key in self.page_lsns:
                    del self.page_lsns[evicted_key]

            del self.frames[evicted_key]
            self.evictions += 1
        self.frames[page_key] = bytes(new_data)
        self.dirty_pages.add(page_key)
        self.access_history.append(page_key)

    def allocate_page(self, type_name: str) -> int:
        return self.disk.allocate_page(type_name)

    def reserve_pages(self, type_name: str, up_to_exclusive: int) -> None:
        self.disk.reserve_pages(type_name, up_to_exclusive)

    def get_stats(self) -> dict:
        return {
            "requests": self.requests,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "dirty_writebacks": self.dirty_writebacks,
        }

    def reset_stats(self):
        self.requests = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.dirty_writebacks = 0