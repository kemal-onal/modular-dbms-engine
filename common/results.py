from dataclasses import dataclass
from typing import Optional, Any, List

@dataclass
class PageResult:
    """DiskSpaceManager ve BufferManager tarafından bir sayfa getirildiğinde döndürülür[cite: 167]."""
    data: bytes
    io_performed: bool  # Gerçek bir disk I/O işlemi yapılıp yapılmadığını belirtir [cite: 171]

@dataclass
class BufferResult:
    """BufferManager tarafından döndürülür[cite: 176]."""
    page: PageResult
    cache_hit: bool
    evicted_page_id: Optional[int]  # Çıkarma işlemi olmadıysa None [cite: 183]
    dirty_writeback: bool           # Çıkarılan sayfa kirliyse True [cite: 184]

@dataclass
class RecordResult:
    """FileIndexManager tarafından arama/menzil işlemleri için döndürülür[cite: 185]."""
    records: List[Any]          # Eşleşen kayıtlar [cite: 192]
    pages_accessed: int
    index_nodes_visited: int    # heap_scan için 0 [cite: 193]
    status: str                 # "success" veya "failure" [cite: 194]

@dataclass
class WriteResult:
    """DiskSpaceManager tarafından yazma işleminde döndürülür[cite: 195]."""
    success: bool
    page_id: int
    old_data: bytes  # Bu yazma işleminden önceki sayfa içeriği [cite: 200]
    new_data: bytes  # Yazılan yeni veri [cite: 200]


@dataclass
class InsertResult:
    """FileIndexManager.insert_record döndürür (ISSUE-15)."""
    status: str        # "success" / "failure"
    page_id: int       # heap page'i
    rec_offset: int    # slot rec_offset


@dataclass
class DeleteResult:
    """FileIndexManager.delete_at döndürür (ISSUE-15)."""
    status: str        # "success" / "not_found"