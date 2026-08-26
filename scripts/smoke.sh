#!/usr/bin/env bash
# End-to-end smoke test against a running server and the real vPIC API.
#
#   Terminal 1:  make run
#   Terminal 2:  ./scripts/smoke.sh
#
# Exercises the full loop for every challenge VIN: cold lookup -> warm lookup
# (must report cached) -> parquet export -> remove -> refetch.

set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
VINS=(
  1HGCM82633A004352
  5YJ3E1EA6PF384836
  1FTFW1ET9DFC10312
  1C4RJFBG2FC625797
  5FNRL6H79NB021411
  1HD1KBM15FB620271
  1XPWD40X1ED215307
)

command -v jq >/dev/null || { echo "this script needs jq"; exit 1; }

echo "== health =="
curl -sS "$BASE/health" | jq -c .

echo
echo "== cold lookups (expect cached_result=false) =="
for vin in "${VINS[@]}"; do
  curl -sS "$BASE/lookup?vin=$vin" | jq -c '{vin, make, model, model_year, body_class, cached_result}'
done

echo
echo "== warm lookups (expect cached_result=true) =="
for vin in "${VINS[@]}"; do
  cached=$(curl -sS -X POST "$BASE/lookup" -H 'content-type: application/json' \
    -d "{\"vin\":\"$vin\"}" | jq -r .cached_result)
  [[ "$cached" == "true" ]] || { echo "FAIL: $vin was not cached"; exit 1; }
  echo "$vin cached ok"
done

echo
echo "== validation (expect 422) =="
for bad in "SHORT" "1HGCM82633A00435!" ""; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/lookup?vin=$bad")
  [[ "$code" == "422" ]] || { echo "FAIL: '$bad' returned $code, expected 422"; exit 1; }
  echo "rejected '$bad' with 422"
done

echo
echo "== export =="
curl -sS -OJ "$BASE/export"
ls -la vin_cache_*.parquet | tail -1
python3 -c "
import glob, pyarrow.parquet as pq
f = sorted(glob.glob('vin_cache_*.parquet'))[-1]
t = pq.read_table(f)
print(f'{f}: {t.num_rows} rows, schema {t.schema.names}')
print(t.to_pandas().to_string(index=False)) if t.num_rows else None
" 2>/dev/null || echo "(install pyarrow to inspect the file)"

echo
echo "== remove + refetch =="
curl -sS -X POST "$BASE/remove" -H 'content-type: application/json' \
  -d "{\"vin\":\"${VINS[0]}\"}" | jq -c .
curl -sS -X POST "$BASE/remove" -H 'content-type: application/json' \
  -d "{\"vin\":\"${VINS[0]}\"}" | jq -c '. + {note: "second delete reports false"}'
curl -sS "$BASE/lookup?vin=${VINS[0]}" | jq -c '{vin, cached_result}'

echo
echo "smoke test passed"
