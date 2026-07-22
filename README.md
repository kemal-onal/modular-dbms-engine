# 🚀 Modular DBMS Engine: Python-based RDBMS with ARIES Recovery

A fully functional, custom-built Relational Database Management System (RDBMS) engine written entirely in pure Python from scratch. This project bypasses high-level frameworks to demonstrate a deep, low-level understanding of database internals, featuring a strict 4-layer architecture, advanced indexing, and robust transaction recovery mechanisms.

No external dependencies. No ORMs. Just raw byte manipulation, buffer pooling, and ACID compliance.

## 🏗️ Architecture Deep Dive

The engine is built with strict separation of concerns, simulating real-world RDBMS architectures via four interacting layers:

1. **Disk Space Manager (`disk_space_manager`):** 
   - Handles raw page I/O operations directly over per-relation `.bin` files. 
   - Manages free-space tracking efficiently using persistent free lists (`<type>_free.bin`) and in-memory high-water marks.
2. **Buffer Manager (`buffer_manager`):** 
   - Custom in-memory buffer pool with configurable `LRU` (Least Recently Used) and `MRU` (Most Recently Used) replacement policies. 
   - Strictly enforces Write-Ahead Logging (WAL) constraints, ensuring dirty pages are never evicted before their corresponding logs are flushed to disk.
3. **File & Index Manager (`file_index_manager`):** 
   - Implements a **Slotted Page** architecture with bitmap occupancy tracking to optimize space utilization for variable-length records. 
   - **Pluggable Indexing:** Seamlessly routes queries through dynamic indexing strategies based on configuration: `heap_scan`, `hash_index` (with overflow chaining), and `bplus_tree` (recursive insertion and range search support).
4. **Query Processor (`query_processor`):** 
   - Parses and executes SQL-like DML/DDL commands (`create`, `search`, `delete`, `range_search`). 
   - Orchestrates transactions (`tx_begin`, `tx_commit`) and features an `explain` command for I/O cost estimation and execution plan transparency.

## 🛡️ ACID Compliance & Crash Recovery (ARIES)

To ensure data durability and atomicity in the event of arbitrary system failures (`crash` command), the engine implements a simplified **ARIES Crash Recovery algorithm**:

* **Write-Ahead Logging (WAL):** All physical modifications and logical operations are appended to `wal.log` before data pages are flushed.
* **3-Phase Recovery on Startup:** Upon restarting from a simulated crash, the engine automatically executes:
  1. **Analysis:** Scans checkpoints and logs to reconstruct the Transaction Table and Dirty Page Table.
  2. **Redo:** Repeats history using physical after-images to restore the system to its exact pre-crash state.
  3. **Undo:** Performs logical undo operations for all uncommitted (loser) transactions, ensuring system consistency without breaking slotted page structures.

## 📊 Performance Testing & Workloads

The project includes built-in benchmarking tools to test the engine under various stress conditions:
* **Workload Generator (`workload_generator.py`):** Generates `sequential`, `random`, `range`, and `mixed` workloads to test buffer hit rates and I/O efficiency.
* **Log Analyzer (`log.csv`):** Tracks and profiles every executed operation, allowing for post-execution metric analysis (e.g., average disk writes per 5000 inserts).

## 🚀 Getting Started

### Prerequisites
This engine is built entirely using Python's standard library. 
* Python 3.8+ 

### Installation
```bash
git clone [https://github.com/kemal-onal/modular-dbms-engine.git](https://github.com/kemal-onal/modular-dbms-engine.git)
cd modular-dbms-engine
```

### Configuration (config.json)
You can tweak the database behavior on the fly without changing the source code. Key parameters include:
  1. "page_size": Size of each disk page in bytes (e.g., 4096).
  2. "buffer_pool_size": Maximum number of pages held in memory.
  3. "replacement_policy": Eviction policy ("LRU" or "MRU").
  4. "index_strategy": heap_scan, hash_index, or bplus_tree.

### Execution
The core entry point is archive.py, requiring a configuration file and an input script:
```bash
python archive.py config.json input.txt
