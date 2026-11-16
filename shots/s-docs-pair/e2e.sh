#!/usr/bin/env bash
# Exploratory end-to-end verification of the documented pairing flow.
# Usage: bash e2e.sh <worktree> <tempdir>
set -u
WT="D:/HermesWorkspace/Vade-wt/s-docs-pair"
TD="$TMPDIR/vade-pair-verify"
PY="D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe"
export PYTHONPATH="$WT/core;$WT/protocol/core/python"

cd "$TD" || exit 1
rm -f out.txt err.txt
rm -rf data

"$PY" -m vade --config core.toml > out.txt 2> err.txt &
CORE_PID=$!
echo "core pid=$CORE_PID"

# poll for the pair_code line, max 20s
for i in $(seq 1 100); do
  if grep -q '^pair_code=' out.txt 2>/dev/null; then break; fi
  sleep 0.2
done

echo "=== STDOUT ==="
cat -n out.txt
echo "=== STDERR ==="
cat -n err.txt

echo "=== /healthz ==="
curl -s -m 5 http://127.0.0.1:8443/healthz; echo

CODE=$(grep '^pair_code=' out.txt | head -1 | sed -E 's/^pair_code=([^ ]+) .*/\1/')
echo "=== extracted code shape ==="
printf '%s\n' "$CODE" | sed -E 's/[^-]-[A-Z0-9]{6}$/<2汉字>-<6位base32>/'

echo "=== POST /ui/session ==="
# Real UTF-8 body written by python: on Windows git-bash, `curl -d` with the
# Chinese code mangles the argument through the ANSI codepage and the JSON
# body then fails to parse ("pair_code is required").
"$PY" -c "import json,sys;open('body.json','w',encoding='utf-8').write(json.dumps({'pair_code':sys.argv[1],'client_name':'my-browser'}))" "$CODE"
curl -s -i -m 5 -X POST http://127.0.0.1:8443/ui/session \
  -H 'content-type: application/json' --data-binary @body.json \
  | sed -E 's/(set-cookie: [A-Za-z0-9_.-]+=)[^;]+/\1<REDACTED>/I'

echo
echo "=== second redeem of same code (must fail: single-use) ==="
curl -s -m 5 -X POST http://127.0.0.1:8443/ui/session \
  -H 'content-type: application/json' --data-binary @body.json; echo

echo "=== kill core ==="
kill "$CORE_PID" 2>/dev/null
wait "$CORE_PID" 2>/dev/null
echo "kill rc=$?"
sleep 1
if kill -0 "$CORE_PID" 2>/dev/null; then echo "STILL ALIVE"; else echo "pid $CORE_PID gone"; fi
echo "=== port 8443 listeners after kill ==="
netstat -ano | grep -E ':8443\s.*LISTENING' || echo "none"
