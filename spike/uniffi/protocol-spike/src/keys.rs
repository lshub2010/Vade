//! Hex + key material loading.
//!
//! Spike rules: private keys live in memory or in a 0600 temp file, never in
//! source, never in logs. `load_client_keys` reads the client static secret
//! from `VADE_SPIKE_CLIENT_SK_FILE` and the server static public key from
//! `VADE_SPIKE_SERVER_PUB` (public, so an env var is fine).

use crate::SpikeError;

pub fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

pub fn hex_decode(s: &str) -> Result<Vec<u8>, SpikeError> {
    let s = s.trim();
    if s.len() % 2 != 0 {
        return Err(SpikeError::config(
            "INVALID_ARGUMENT",
            "hex string has odd length",
        ));
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(s.len() / 2);
    let val = |c: u8| -> Result<u8, SpikeError> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            b'A'..=b'F' => Ok(c - b'A' + 10),
            _ => Err(SpikeError::config(
                "INVALID_ARGUMENT",
                "hex string contains a non-hex character",
            )),
        }
    };
    for pair in bytes.chunks(2) {
        out.push((val(pair[0])? << 4) | val(pair[1])?);
    }
    Ok(out)
}

/// Zero a secret buffer after it has been handed to the Noise state machine.
///
/// Note: snow keeps its own copy inside `Toggle<Box<dyn Dh>>`; that copy is not
/// wiped here (see RESULT.md, "对 C1 的约束"). This only avoids leaving extra
/// copies in our own allocations longer than necessary.
pub fn wipe(buf: &mut [u8]) {
    for b in buf.iter_mut() {
        // volatile-ish write so the compiler does not elide it
        unsafe { std::ptr::write_volatile(b, 0) };
    }
}

pub struct ClientKeys {
    pub client_sk: Vec<u8>,
    pub server_pub: Vec<u8>,
}

pub fn load_client_keys() -> Result<ClientKeys, SpikeError> {
    let server_pub_hex = std::env::var("VADE_SPIKE_SERVER_PUB").map_err(|_| {
        SpikeError::config(
            "INVALID_ARGUMENT",
            "VADE_SPIKE_SERVER_PUB is not set (hex of the server static public key)",
        )
    })?;
    let server_pub = hex_decode(&server_pub_hex)?;

    let sk_path = std::env::var("VADE_SPIKE_CLIENT_SK_FILE").map_err(|_| {
        SpikeError::config(
            "INVALID_ARGUMENT",
            "VADE_SPIKE_CLIENT_SK_FILE is not set (path to a 0600 hex file)",
        )
    })?;
    let raw = std::fs::read_to_string(&sk_path).map_err(|e| {
        SpikeError::config(
            "INVALID_ARGUMENT",
            format!("cannot read VADE_SPIKE_CLIENT_SK_FILE: {e}"),
        )
    })?;
    let client_sk = hex_decode(&raw)?;

    if client_sk.len() != 32 {
        return Err(SpikeError::config(
            "INVALID_ARGUMENT",
            format!("client static secret must be 32 bytes, got {}", client_sk.len()),
        ));
    }
    if server_pub.len() != 32 {
        return Err(SpikeError::config(
            "INVALID_ARGUMENT",
            format!("server static public must be 32 bytes, got {}", server_pub.len()),
        ));
    }
    Ok(ClientKeys {
        client_sk,
        server_pub,
    })
}

/// Test/demo helper: write a key file with mode 0600 on unix.
pub fn write_secret_file(path: &std::path::Path, hex: &str) -> std::io::Result<()> {
    use std::io::Write;
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(path)?;
    f.write_all(hex.as_bytes())?;
    f.write_all(b"\n")?;
    f.flush()
}
