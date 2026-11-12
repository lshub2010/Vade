#!/usr/bin/env bash
# C0b T1: build the protocol core for wasm32-unknown-unknown and emit an ES module pkg/.
#
# Requires:
#   rustup target add wasm32-unknown-unknown
#   wasm-bindgen-cli == the wasm-bindgen version pinned in Cargo.lock (0.2.130)
# Install the CLI either with:
#   cargo install wasm-bindgen-cli --version 0.2.130 --locked
#   (or use the prebuilt release tarball; ~8 MB, same binary)
set -euo pipefail
cd "$(dirname "$0")"

WASM_BINDGEN="${WASM_BINDGEN:-wasm-bindgen}"
if ! command -v "$WASM_BINDGEN" >/dev/null 2>&1; then
  echo "error: wasm-bindgen not found on PATH (set WASM_BINDGEN=/path/to/wasm-bindgen)" >&2
  exit 1
fi

# Pin the target dir. A CARGO_TARGET_DIR exported by another spike (C0a/UniFFI)
# silently redirects `cargo build`, while wasm-bindgen below reads ./target — the
# result is that pkg/ is generated from a stale .wasm and the Rust changes never
# reach the browser. Hard-coding the dir makes the build reproducible.
TARGET_DIR="${TARGET_DIR:-target}"
mkdir -p "$TARGET_DIR"
cargo build --target wasm32-unknown-unknown --release --target-dir "$TARGET_DIR"

WASM="$TARGET_DIR/wasm32-unknown-unknown/release/protocol_wasm.wasm"
if [ ! -f "$WASM" ]; then
  echo "error: expected artifact not found: $WASM" >&2
  exit 1
fi

rm -rf pkg
"$WASM_BINDGEN" --target web --no-typescript \
  --out-dir pkg \
  "$WASM"

# Wrap pkg/ as an npm package so Kotlin/Wasm's webpack resolve can `import "protocol-wasm"`.
cat > pkg/package.json <<'JSON'
{
  "name": "protocol-wasm",
  "version": "0.1.0",
  "type": "module",
  "main": "protocol_wasm.js",
  "exports": {
    ".": "./protocol_wasm.js",
    "./wasm-b64.js": "./wasm-b64.js"
  }
}
JSON

# Browsers/Node cannot `fetch()` a file:// URL, and webpack would rewrite
# `new URL(..., import.meta.url)` into a server asset path. Embedding the module
# as base64 keeps the spike's single loading path identical in Node and browser.
node -e '
const fs = require("fs");
const b64 = fs.readFileSync("pkg/protocol_wasm_bg.wasm").toString("base64");
fs.writeFileSync("pkg/wasm-b64.js", "export const WASM_BASE64 = " + JSON.stringify(b64) + ";\n");
console.log("wasm-b64.js bytes:", b64.length);
'

echo "--- artifacts ---"
ls -l pkg
