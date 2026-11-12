//! Generate throwaway key material for the demo. NOT for production.
//!
//! Writes four files into `VADE_SPIKE_KEY_DIR` (default /tmp/vade-spike-keys):
//!   server_static.sk.hex  (0600)   server_static.pub.hex
//!   client_static.sk.hex  (0600)   client_static.pub.hex
//!
//! Only public keys are printed. Secrets stay in 0600 files and are loaded into
//! memory by the client/server processes.

use protocol_spike::{client, keys, server};
use std::path::PathBuf;

fn main() {
    let raw = std::env::args().nth(1);
    if let Some(a) = raw.as_deref() {
        if a.starts_with('-') {
            eprintln!("usage: spike-fixture [KEY_DIR]");
            std::process::exit(2);
        }
    }
    let dir = raw
        .or_else(|| std::env::var("VADE_SPIKE_KEY_DIR").ok())
        .unwrap_or_else(|| "/tmp/vade-spike-keys".to_string());
    let dir = PathBuf::from(dir);
    if let Err(e) = std::fs::create_dir_all(&dir) {
        eprintln!("FIXTURE_ERROR cannot create {}: {e}", dir.display());
        std::process::exit(1);
    }

    let params = match client::params() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("FIXTURE_ERROR {e}");
            std::process::exit(1);
        }
    };

    for name in ["server_static", "client_static"] {
        let kp = match snow::Builder::new(params.clone()).generate_keypair() {
            Ok(k) => k,
            Err(e) => {
                eprintln!("FIXTURE_ERROR keygen {name}: {e:?}");
                std::process::exit(1);
            }
        };
        let pub_path = dir.join(format!("{name}.pub.hex"));
        let sk_path = dir.join(format!("{name}.sk.hex"));
        if let Err(e) = std::fs::write(&pub_path, format!("{}\n", keys::hex_encode(&kp.public))) {
            eprintln!("FIXTURE_ERROR write {}: {e}", pub_path.display());
            std::process::exit(1);
        }
        if let Err(e) = keys::write_secret_file(&sk_path, &keys::hex_encode(&kp.private)) {
            eprintln!("FIXTURE_ERROR write {}: {e}", sk_path.display());
            std::process::exit(1);
        }
        // Sanity: the public half must match what the server will derive.
        match server::public_from_secret(&kp.private) {
            Ok(derived) if derived == kp.public => {}
            _ => {
                eprintln!("FIXTURE_ERROR derived public key mismatch for {name}");
                std::process::exit(1);
            }
        }
    }

    println!("KEY_DIR={}", dir.display());
}
