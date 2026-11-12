//! UniFFI binding generator entry point (C1 T5).
//!
//!   cargo run --features uniffi --bin uniffi-bindgen -- \
//!     generate --library target/debug/libvade_protocol.so \
//!     --language python --out-dir python

fn main() {
    uniffi::uniffi_bindgen_main()
}
