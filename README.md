# Modular DBMS Engine with ARIES Recovery

A custom-built, modular Relational Database Management System (RDBMS) engine written entirely in Python from scratch. This project demonstrates the core internal mechanisms of a database, featuring a 4-layer architecture and a robust crash recovery system.

## 🏗️ Architecture

The engine is built with strict separation of concerns, divided into four interacting layers:

1. **Disk Space Manager:** Handles raw page I/O operations over per-relation `.bin` files and manages free-space tracking using bitmaps/free lists.
2. **Buffer Manager:** Manages the in-memory buffer pool with configurable replacement policies (LRU/MRU) and ensures Write-Ahead Logging (WAL) rules are enforced before page evictions.
3. **File & Index Manager:** Manages record storage using slotted pages. Supports dynamic indexing strategies including **B+ Trees** and **Static Hashing** for efficient point and range queries.
4. **Query Processor:** Parses and executes SQL-like DML/DDL commands, orchestrating transactions and estimating I/O costs for query execution plans.

## 🛡️ Crash Recovery (Write-Ahead Logging & ARIES)

To ensure data durability and atomicity, the engine implements a simplified **ARIES Crash Recovery** algorithm combined with **Logical Command Replay**:

* **Write-Ahead Logging (WAL):** All database modifications are logged before the actual data pages are flushed to disk.
* **3-Phase Recovery:** Upon restarting from an unexpected crash, the engine performs Analysis, Redo, and Undo phases.
* **Logical Replay:** To avoid physical byte-level conflicts during interleaved transactions, the recovery manager gracefully replays only the committed (winner) transaction logs, ensuring a 100% consistent database state.

## ⚙️ Features

* **ACID Compliance:** Transaction tracking (`tx_begin`, `tx_commit`) with full rollback capabilities on system failure.
* **Pluggable Indexes:** Seamless switching between Heap Scan, B+ Tree, and Hash indexing via configuration.
* **Slotted Page Design:** Efficient space utilization within pages for variable and fixed-length records.
* **System Catalog:** Persistent schema tracking for dynamically created types and tables.

## 🚀 Getting Started

### Prerequisites
This engine is built entirely using Python's standard library. No external dependencies or heavy frameworks are required.
* Python 3.8+

### Installation
Clone the repository to your local environment:
```bash
git clone [https://github.com/kemal-onal/modular-dbms-engine.git](https://github.com/kemal-onal/modular-dbms-engine.git)
cd modular-dbms-engine
```

### Configuration (`config.json`)

The engine's architecture is highly configurable. You can tweak the database behavior on the fly by editing the `config.json` file. Key parameters include:

* **`"page_size"`:** Size of each disk page in bytes (e.g., 4096).
* **`"buffer_pool_size"`:** Maximum number of pages held in memory.
* **`"replacement_policy"`:** Eviction policy, either `"LRU"` or `"MRU"`.
* **`"index_strategy"`:** Indexing mechanism, choose between `"heap_scan"`, `"hash_index"`, or `"bplus_tree"`.

### Running the Engine

The core entry point is `archive.py`. It requires a configuration file and an input file containing SQL-like commands or transactions.

```bash
# Basic execution
python archive.py config.json input.txt'
```

### Testing Crash Recovery (ARIES)

To observe the Write-Ahead Logging and ARIES crash recovery in action, run a transaction scenario that ends with a forced `crash` command, then restart the engine to trigger the automated recovery process:

```bash
# 1. Run a transaction that crashes the system mid-way
python archive.py config.json test_cases/case_1/input_a.txt

# 2. Restart the engine to initiate Phase 1 (Analysis), Phase 2 (Redo), and Phase 3 (Undo)
python archive.py config.json test_cases/case_1/input_b.txt
```

### Outputs

* The output of all read operations (e.g., `search record`, `range_search`) will be written to `output.txt`.
* Transaction execution statuses are logged in `log.csv`.
* Write-Ahead Logs (Logical Commands) are securely flushed to `wal.log`.