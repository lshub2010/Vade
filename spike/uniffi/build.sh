#!/usr/bin/env bash
# C0a spike · one-shot rebuild + verification.
#
#   ./build.sh              # rust tests -> kotlin bindings -> wheel -> kotlin demo -> python demo
#   SKIP_KOTLIN=1 ./build.sh  # skip the Gradle demo (slow on small machines)
#
# Every step prints a STEP= marker so a log can be diffed without reading prose.
# Nothing here talks to a network except Gradle/Cargo dependency resolution; the
# bindings and the wheel are generated locally.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CRATE="$HERE/protocol-spike"
KOTLIN="$HERE/kotlin"
PY="$HERE/python"
DIST="$HERE/dist"
PYBIN=${PYBIN:-python3}

export PATH="${CARGO_HOME:-$HOME/.cargo}/bin:$PATH"
command -v cargo >/dev/null || { echo "STEP=fail reason=cargo-not-on-PATH"; exit 1; }

step() { echo "STEP=$*"; }

step rust-tests
(cd "$CRATE" && cargo test --lib --quiet)

step rust-build
(cd "$CRATE" && cargo build --bin spike-server --bin spike-fixture --lib)

step uniffi-bindgen-kotlin
(cd "$CRATE" && cargo run -q --bin uniffi-bindgen -- generate \
    --library target/debug/libprotocol_spike.so \
    --language kotlin --out-dir "$KOTLIN/src/main/kotlin")

step uniffi-bindgen-python
(cd "$CRATE" && cargo run -q --bin uniffi-bindgen -- generate \
    --library target/debug/libprotocol_spike.so \
    --language python --out-dir "$PY")

step wheel
# A debug cdylib is 80+ MB; build the release one for the wheel when possible.
(cd "$CRATE" && cargo build --release --lib)
"$PYBIN" "$PY/build_wheel.py" \
    --lib "$CRATE/target/release/libprotocol_spike.so" \
    --module "$PY/protocol_spike.py" \
    --out "$DIST"

step python-clean-venv
VENV=${VENV:-/tmp/c0a-venv}
rm -rf "$VENV"
"$PYBIN" -m venv "$VENV"
"$VENV/bin/pip" install --no-index --disable-pip-version-check -q "$DIST"/*.whl
KEYDIR=${KEYDIR:-/tmp/vade-spike-keys}
rm -rf "$KEYDIR"
"$CRATE/target/release/spike-fixture" "$KEYDIR"
"$VENV/bin/python" "$PY/demo.py" \
    --server "$CRATE/target/release/spike-server" --key-dir "$KEYDIR"

if [ "${SKIP_KOTLIN:-0}" != "1" ]; then
  step kotlin-demo
  (cd "$KOTLIN" && ./gradlew run --console=plain)
fi

step done
