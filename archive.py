# archive.py
import sys
import json
from disk_space_manager import DiskSpaceManager
from buffer_manager import BufferManager
from file_index_manager import FileIndexManager
from query_processor import QueryProcessor
from recovery_manager import RecoveryManager

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 archive.py config.json input.txt")
        return

    config_path = sys.argv[1]
    input_path = sys.argv[2]

    # Konfigürasyonu yükle
    with open(config_path) as cf:
        config = json.load(cf)

    # Katmanları bottom-up sırayla inşa et 
    disk = DiskSpaceManager(config)
    buffer = BufferManager(config, disk)
    file_idx = FileIndexManager(config, buffer)
    qp = QueryProcessor(config, file_idx, buffer, disk)

    recovery = RecoveryManager(config, disk)
    buffer.recovery_manager = recovery
    file_idx.recovery_manager = recovery
    qp.recovery_manager = recovery

    # Startup'ta uc fazli recovery: input islenmeden once tutarli state'i kur
    recovery.recover(qp)

    # Girdi dosyasını satır satır işle
    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if line:
                qp.process(line)
    
    # İşlemler bitince buffer'daki kirli sayfaları diske yaz 
    buffer.flush()

if __name__ == "__main__":
    main()