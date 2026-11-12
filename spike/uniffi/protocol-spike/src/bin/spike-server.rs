//! Standalone test server used by the Python / Kotlin demos.
//!
//!   VADE_SPIKE_KEY_DIR=/tmp/vade-spike-keys cargo run --release --bin spike-server
//!
//! Prints exactly two machine-readable lines, then serves until killed:
//!   LISTEN=127.0.0.1:PORT
//!   SERVER_PUB=<hex>
//! No secret is ever printed.

use protocol_spike::{keys, server};
use std::path::PathBuf;

fn main() {
    let dir = std::env::var("VADE_SPIKE_KEY_DIR").unwrap_or_else(|_| "/tmp/vade-spike-keys".into());
    let dir = PathBuf::from(dir);

    let server_sk = read_hex(&dir.join("server_static.sk.hex"));
    let client_pub = read_hex(&dir.join("client_static.pub.hex"));
    let (server_sk, client_pub) = match (server_sk, client_pub) {
        (Ok(a), Ok(b)) => (a, b),
        (a, b) => {
            let err = a.err().or(b.err()).unwrap();
            eprintln!("SERVER_ERROR {err}");
            std::process::exit(1);
        }
    };

    let handle = match server::start_server(&server_sk, &client_pub, server::responder_prologue()) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("SERVER_ERROR {e}");
            std::process::exit(1);
        }
    };

    println!("LISTEN={}", handle.addr);
    println!("SERVER_PUB={}", keys::hex_encode(&handle.static_public));
    use std::io::Write;
    let _ = std::io::stdout().flush();

    // Serve forever; the demo harness kills us.
    loop {
        std::thread::park();
    }
}

fn read_hex(path: &std::path::Path) -> Result<Vec<u8>, String> {
    let raw = std::fs::read_to_string(path)
        .map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    keys::hex_decode(&raw).map_err(|e| format!("{}: {e}", path.display()))
}
