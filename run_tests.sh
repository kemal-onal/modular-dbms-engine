#!/bin/bash
# Her test case'i ayri bir scratch cwd'de kosar; data dosyalari (.bin/wal.log/
# master.log) orada birikir, recovery dosyalar arasinda calisir. output.txt
# archive.py'nin yaninda (repo root) olusur ve her kosumda truncate edilir, bu
# yuzden yalniz son verify kosumunun ciktisini expected ile karsilastiririz.
set -u
REPO="$(cd "$(dirname "$0")" && pwd)"
CASES_DIR="$REPO/test_cases"
PASS=0
FAIL=0

for case_dir in "$CASES_DIR"/case_*; do
    name="$(basename "$case_dir")"
    scratch="$(mktemp -d)"
    cp "$case_dir/config.json" "$scratch/config.json"

    # Lexicographic sirada tum input dosyalari, sonra verify
    inputs=$(ls "$case_dir"/input_*.txt 2>/dev/null | sort)
    for inp in $inputs; do
        ( cd "$scratch" && python3 "$REPO/archive.py" "$scratch/config.json" "$inp" ) >/dev/null 2>&1
    done
    ( cd "$scratch" && python3 "$REPO/archive.py" "$scratch/config.json" "$case_dir/verify.txt" ) >/dev/null 2>&1

    if diff -q "$REPO/output.txt" "$case_dir/expected_output.txt" >/dev/null 2>&1; then
        echo "PASS  $name"
        PASS=$((PASS+1))
    else
        echo "FAIL  $name"
        FAIL=$((FAIL+1))
        echo "----- diff (expected vs got), first 30 lines -----"
        diff "$case_dir/expected_output.txt" "$REPO/output.txt" | head -30
        echo "--------------------------------------------------"
    fi
    rm -rf "$scratch"
done

echo ""
echo "Result: $PASS passed, $FAIL failed"
