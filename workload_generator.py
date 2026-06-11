# workload_generator.py
"""
Generates DBMS workload scripts for the four modes required by the spec
(Section 10): sequential, random, range, mixed.

Usage:
    python3 workload_generator.py --mode MODE --records N --queries Q > workload.txt

The generated type has 4 fields with 2 integer fields (spec minimum).
Output is written to stdout so it can be redirected to any file name.
"""
import argparse
import random
import string
import sys


TYPE_NAME = "person"
SCHEMA_LINE = f"create type {TYPE_NAME} 4 1 id int name str age int city str"


def _random_name(rng: random.Random) -> str:
    return "".join(rng.choices(string.ascii_lowercase, k=rng.randint(5, 10))).capitalize()


def _random_city(rng: random.Random) -> str:
    return f"City{rng.randint(1, 999)}"


def _emit_insert(rng: random.Random, pk: int):
    print(f"create record {TYPE_NAME} {pk} {_random_name(rng)} {rng.randint(18, 90)} {_random_city(rng)}")


def _emit_inserts(rng: random.Random, ids: list):
    for pk in ids:
        _emit_insert(rng, pk)


def gen_sequential(rng, records: int, queries: int):
    """Insert 1..N in order, then Q full-table SELECT-style range scans."""
    _emit_inserts(rng, list(range(1, records + 1)))
    for _ in range(queries):
        print(f"range_search {TYPE_NAME} id 1 {records}")


def gen_random(rng, records: int, queries: int):
    """Insert in random PK order, then Q random PK equality searches."""
    ids = list(range(1, records + 1))
    rng.shuffle(ids)
    _emit_inserts(rng, ids)
    for _ in range(queries):
        print(f"search record {TYPE_NAME} {rng.randint(1, records)}")


def gen_range(rng, records: int, queries: int):
    """Insert in random PK order, then Q random range queries on the int `age` field."""
    ids = list(range(1, records + 1))
    rng.shuffle(ids)
    _emit_inserts(rng, ids)
    for _ in range(queries):
        low = rng.randint(18, 80)
        high = low + rng.randint(1, 10)
        print(f"range_search {TYPE_NAME} age {low} {high}")


def gen_mixed(rng, records: int, queries: int):
    """Insert N, then Q operations mixing search / range_search / delete / insert."""
    ids = list(range(1, records + 1))
    rng.shuffle(ids)
    _emit_inserts(rng, ids)
    # Track live PKs so delete/search aim at existing rows most of the time.
    live = set(ids)
    next_pk = records + 1
    for _ in range(queries):
        op = rng.choice(["search", "range", "delete", "insert"])
        if op == "search" and live:
            print(f"search record {TYPE_NAME} {rng.choice(list(live))}")
        elif op == "range":
            low = rng.randint(18, 80)
            high = low + rng.randint(1, 20)
            print(f"range_search {TYPE_NAME} age {low} {high}")
        elif op == "delete" and live:
            pk = rng.choice(list(live))
            print(f"delete record {TYPE_NAME} {pk}")
            live.remove(pk)
        else:
            _emit_insert(rng, next_pk)
            live.add(next_pk)
            next_pk += 1


MODES = {
    "sequential": gen_sequential,
    "random": gen_random,
    "range": gen_range,
    "mixed": gen_mixed,
}


def main(argv=None):
    p = argparse.ArgumentParser(description="DBMS workload generator (Spec Section 10).")
    p.add_argument("--mode", choices=sorted(MODES), required=True)
    p.add_argument("--records", type=int, required=True, help="number of INSERT records")
    p.add_argument("--queries", type=int, required=True, help="number of follow-up queries")
    p.add_argument("--seed", type=int, default=42, help="RNG seed (default 42 for reproducibility)")
    args = p.parse_args(argv)

    rng = random.Random(args.seed)
    print(SCHEMA_LINE)
    MODES[args.mode](rng, args.records, args.queries)


if __name__ == "__main__":
    main()
