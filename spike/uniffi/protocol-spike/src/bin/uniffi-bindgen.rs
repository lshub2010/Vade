//! Standard UniFFI bindgen entry point, pinned to the version in Cargo.toml.
//!
//!   cargo run --bin uniffi-bindgen -- generate --library <cdylib> --language python --out-dir ...
fn main() {
    uniffi::uniffi_bindgen_main();
}
