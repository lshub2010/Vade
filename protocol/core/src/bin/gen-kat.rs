//! Generate `protocol/v1/kat.json` from this implementation (contract C-02).
//!
//! Run from `protocol/core`:
//!
//! ```text
//! cargo run --features kat-gen --bin gen-kat
//! ```
//!
//! Every value in the file is **computed here**, never transcribed. `source`
//! records provenance: `generated` means "output of this program". The frame
//! bodies are real ChaCha20-Poly1305 output, so a second implementation can both
//! parse the frame and decrypt its body.

use std::path::Path;

use chacha20poly1305::aead::{Aead, KeyInit};
use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use vade_protocol::frame::{Frame, Limits, TAG_LEN};
use vade_protocol::handshake::{prologue, ConnectionRole};
use vade_protocol::identity::Identity;
use vade_protocol::paircode::{
    derive_pair_psk, normalize_pair_code, PAIR_PSK_INFO, PAIR_PSK_SALT,
};
use vade_protocol::frame::FrameType;

/// KAT AEAD key: 0x00..=0x1f.
fn kat_key() -> [u8; 32] {
    core::array::from_fn(|i| i as u8)
}

/// Fixed nonce for frame bodies. Documented as a KAT constant only — it has no
/// relationship to the Noise nonce, which the transport never derives from the
/// frame counter.
const KAT_NONCE: [u8; 12] = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1];

fn pattern(len: usize) -> Vec<u8> {
    (0..len).map(|i| (i % 251) as u8).collect()
}

fn aead_seal(key: &[u8; 32], nonce: &[u8; 12], pt: &[u8]) -> Vec<u8> {
    let cipher = ChaCha20Poly1305::new(Key::from_slice(key));
    cipher
        .encrypt(Nonce::from_slice(nonce), pt)
        .expect("KAT encryption cannot fail")
}

fn frame_vector(
    name: &str,
    typ: FrameType,
    generation: u32,
    counter: u64,
    payload_len: usize,
) -> Value {
    let (body, note) = if typ.is_plaintext() {
        (
            pattern(payload_len),
            "body is a plaintext Noise message stand-in (deterministic pattern)".to_string(),
        )
    } else {
        let pt = pattern(payload_len);
        (
            aead_seal(&kat_key(), &KAT_NONCE, &pt),
            format!(
                "body = ChaCha20-Poly1305(kat_key, nonce {}, zero-aad, pt=pattern({payload_len}))",
                hex::encode(KAT_NONCE)
            ),
        )
    };
    let frame = Frame::from_body(typ, generation, counter, body).expect("valid KAT frame");
    let wire = frame.encode();
    // The vector must round-trip through our own decoder before it is written.
    let back = Frame::decode(&wire, &Limits::default()).expect("KAT frame must decode");
    assert_eq!(back, frame);

    json!({
        "name": name,
        "ver": wire[0],
        "type": wire[1],
        "flags": wire[2],
        "rsv": wire[3],
        "generation": generation,
        "counter": counter,
        "payload_len": payload_len,
        "body_len": frame.body().len(),
        "frame_len": wire.len(),
        "hex": hex::encode(&wire),
        "note": note,
        "source": "generated",
    })
}

fn aead_vector(name: &str, key: &[u8; 32], nonce: &[u8; 12], pt: &[u8]) -> Value {
    let ct = aead_seal(key, nonce, pt);
    json!({
        "name": name,
        "key": hex::encode(key),
        "nonce": hex::encode(nonce),
        "pt": hex::encode(pt),
        "ct": hex::encode(&ct),
        "tag_len": TAG_LEN,
        "aad": "",
        "source": "generated",
    })
}

fn main() {
    let core_secret = [0x11u8; 32];
    let core = Identity::from_secret_bytes(&core_secret).expect("fixed KAT key");
    let core_pub = core.public_key();
    let core_fp = core.fingerprint();

    // ---- frames -----------------------------------------------------------
    let frames = vec![
        frame_vector("handshake-msg1-96", FrameType::Handshake, 0, 0, 96),
        frame_vector("rpc-1kb", FrameType::Rpc, 1, 1, 1024),
        frame_vector("rpc-empty", FrameType::Rpc, 1, 2, 0),
        frame_vector("event-64", FrameType::Event, 1, 7, 64),
        frame_vector("heartbeat-16", FrameType::Heartbeat, 1, 12_345, 16),
        frame_vector("close-32", FrameType::Close, 1, 999, 32),
        frame_vector("rekey-gen1-99", FrameType::Rekey, 1, 99, 4),
        frame_vector("rpc-gen7-big-counter", FrameType::Rpc, 7, (1u64 << 40) + 1, 8),
        frame_vector("rpc-gen1-max-counter", FrameType::Rpc, 1, u64::MAX - 1024, 4),
    ];

    // ---- aead -------------------------------------------------------------
    let aeads = vec![
        aead_vector("chacha20poly1305-empty", &kat_key(), &[0u8; 12], &[]),
        aead_vector("chacha20poly1305-1b", &kat_key(), &KAT_NONCE, &[0x42]),
        aead_vector(
            "chacha20poly1305-16b",
            &kat_key(),
            &[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2],
            &pattern(16),
        ),
        aead_vector(
            "chacha20poly1305-1kb",
            &kat_key(),
            &[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3],
            &pattern(1024),
        ),
    ];

    // ---- pair-code normalisation + PSK ------------------------------------
    let inputs = [
        ("pair-psk", "青松-A7K2M9"),
        ("pair-psk-fullwidth", "青松－Ａ７Ｋ２Ｍ９"),
        ("pair-psk-lower", "青松 a7k2m9"),
        ("pair-psk-middle-dot", "青松·A7K2M9"),
    ];
    let mut hkdf = Vec::new();
    let mut psk_ref: Option<[u8; 32]> = None;
    let mut normalize = Vec::new();
    for (name, code) in inputs {
        let normalized = normalize_pair_code(code).expect("valid KAT pair code");
        let psk = derive_pair_psk(code, &core_pub).expect("PSK derivation");
        if let Some(prev) = psk_ref {
            assert_eq!(prev, psk, "equivalent pair codes must derive the same PSK");
        }
        psk_ref = Some(psk);
        normalize.push(json!({
            "name": format!("normalize-{}", name.trim_start_matches("pair-psk").trim_start_matches('-')),
            "input": code,
            "expected": normalized,
        }));
        hkdf.push(json!({
            "name": name,
            "pair_code": code,
            "normalized": normalized,
            "core_pub": hex::encode(&core_pub),
            "core_fp": hex::encode(&core_fp),
            "salt": String::from_utf8_lossy(PAIR_PSK_SALT),
            "info": String::from_utf8_lossy(PAIR_PSK_INFO),
            "psk": hex::encode(psk),
            "source": "generated",
        }));
    }

    // ---- prologue contract -------------------------------------------------
    let prologues = json!([
        {"connection_role": "core_initiated", "byte": 1, "bytes_hex": hex::encode(prologue(ConnectionRole::CoreInitiated))},
        {"connection_role": "module_initiated", "byte": 2, "bytes_hex": hex::encode(prologue(ConnectionRole::ModuleInitiated))},
    ]);

    let doc = json!({
        "version": 1,
        "protocol": "vade/v1",
        "generator": "protocol/core/src/bin/gen-kat.rs",
        "how_generated": "cargo run --features kat-gen --bin gen-kat (every vector is computed by this build; nothing is transcribed)",
        "crate_version": vade_protocol::version(),
        "notes": [
            "frame.hex is the complete wire frame: 20-byte header + body. Non-handshake bodies are real ChaCha20-Poly1305 output over pattern(payload_len) with the kat key and the nonce in `note`.",
            "aead.aad is always empty because Noise's cipherstate does not authenticate a header.",
            "hkdf.psk = HKDF-SHA256(ikm = utf8(normalized) || BLAKE2s256(core_pub), salt = salt, info = empty, L = 32).",
            "prologue = b\"vade/v1\" || role_byte; both peers of one connection MUST send the same connection_role.",
        ],
        "frame": frames,
        "aead": aeads,
        "hkdf": hkdf,
        "normalize": normalize,
        "prologue": prologues,
    });

    let text = serde_json::to_string_pretty(&doc).expect("serialize kat") + "\n";
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../v1/kat.json");
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).expect("create protocol/v1");
    }
    std::fs::write(&path, &text).expect("write kat.json");

    let digest = Sha256::digest(text.as_bytes());
    println!("wrote {} ({} bytes)", path.display(), text.len());
    println!("sha256(cat) = {}", hex::encode(digest));
    println!(
        "frames={} aead={} hkdf={} normalize={}",
        doc["frame"].as_array().map_or(0, Vec::len),
        doc["aead"].as_array().map_or(0, Vec::len),
        doc["hkdf"].as_array().map_or(0, Vec::len),
        doc["normalize"].as_array().map_or(0, Vec::len),
    );
}
