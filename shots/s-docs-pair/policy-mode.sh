#!/usr/bin/env bash
# Verifies the /healthz policy_mode expectation from the client QUICKSTART §6.
# Claim under test: "QUICKSTART expects policy_mode=allow_all but a minimal
# config yields default". The QUICKSTART's own core.toml sets [dev] allow_all.
set -u
WT="D:/HermesWorkspace/Vade-wt/s-docs-pair"
PY="D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe"
TD="$TMPDIR/allowall-test"
export PYTHONPATH="$WT/core;$WT/protocol/core/python"

rm -rf "$TD"; mkdir -p "$TD"; cd "$TD" || exit 1
cat > core.toml <<'EOF'
[core]
listen = "127.0.0.1:8010"
admin_host = "127.0.0.1:0"
data_dir = "./data"
log_level = "warning"

[dev]
allow_all = true
allow_missing_components = true
EOF

"$PY" -m vade --config core.toml > o.txt 2> e.txt &
PID=$!
for i in $(seq 1 150); do grep -q '^pair_code=' o.txt 2>/dev/null && break; sleep 0.2; done

echo "=== /healthz (QUICKSTART's own core.toml, [dev] allow_all = true) ==="
curl -s -m 5 http://127.0.0.1:8010/healthz; echo
echo "=== stdout ==="
cat -n o.txt
echo "=== stderr ==="
cat -n e.txt

kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; sleep 1
echo "=== 8010 listeners after teardown ==="
netstat -ano | grep -E ':8010\s.*LISTENING' || echo "  none"
