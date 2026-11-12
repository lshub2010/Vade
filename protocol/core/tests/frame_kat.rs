//! Verifies `protocol/v1/kat.json` against this implementation.
//!
//! If any of these fail, either the wire format changed (bump the protocol
//! version and regenerate) or the KAT file was edited by hand.

use std::path::PathBuf;

use chacha20poly1305::aead::{Aead, KeyInit};
use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};
use vade_protocol::frame::{Frame, FrameHeader, Limits, TAG_LEN};
use vade_protocol::identity::fingerprint_of;
use vade_protocol::kat::Kat;
use vade_protocol::paircode::{derive_pair_psk, normalize_pair_code};

fn kat_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../v1/kat.json")
}

fn load() -> Kat {
    let text = std::fs::read_to_string(kat_path()).expect("protocol/v1/kat.json must exist");
    Kat::parse(&text).expect("kat.json must parse")
}

#[test]
fn kat_has_the_expected_coverage() {
    let kat = load();
    assert_eq!(kat.version, 1);
    assert_eq!(kat.protocol, "vade/v1");
    assert!(kat.frame.len() >= 8, "frames: {}", kat.frame.len());
    assert!(kat.aead.len() >= 4, "aead: {}", kat.aead.len());
    assert!(kat.hkdf.len() >= 4, "hkdf: {}", kat.hkdf.len());
    assert!(kat.normalize.len() >= 4, "normalize: {}", kat.normalize.len());
    for v in &kat.frame {
        assert_eq!(v.source, "generated", "{} must not be hand-written", v.name);
    }
    for v in &kat.aead {
        assert_eq!(v.source, "generated", "{} must not be hand-written", v.name);
    }
    for v in &kat.hkdf {
        assert_eq!(v.source, "generated", "{} must not be hand-written", v.name);
    }
}

#[test]
fn frame_vectors_decode_and_re_encode_byte_for_byte() {
    let kat = load();
    for v in &kat.frame {
        let bytes = Kat::frame_bytes(v).unwrap();
        let header = FrameHeader::peek(&bytes).unwrap();
        assert_eq!(header.version, v.ver, "{}", v.name);
        assert_eq!(header.typ.as_u8(), v.typ, "{}", v.name);
        assert_eq!(header.flags, v.flags, "{}", v.name);
        assert_eq!(header.reserved, v.rsv, "{}", v.name);
        assert_eq!(header.generation, v.generation, "{}", v.name);
        assert_eq!(header.counter, v.counter, "{}", v.name);
        assert_eq!(header.payload_len, v.payload_len, "{}", v.name);
        assert_eq!(header.wire_len().unwrap(), bytes.len(), "{}", v.name);

        let frame = Frame::decode(&bytes, &Limits::default()).unwrap();
        assert_eq!(frame.encode(), bytes, "{} round-trip", v.name);
    }
}

#[test]
fn frame_vectors_carry_real_aead_bodies() {
    let kat = load();
    // Non-handshake bodies are ChaCha20-Poly1305 over pattern(payload_len) with
    // the KAT key and nonce 000000000000000000000001.
    let key: [u8; 32] = core::array::from_fn(|i| i as u8);
    let nonce: [u8; 12] = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1];
    let cipher = ChaCha20Poly1305::new(Key::from_slice(&key));
    let mut checked = 0;
    for v in &kat.frame {
        if v.typ == 1 {
            continue; // handshake frames are plaintext by design
        }
        let bytes = Kat::frame_bytes(v).unwrap();
        let frame = Frame::decode(&bytes, &Limits::default()).unwrap();
        let pt = cipher
            .decrypt(Nonce::from_slice(&nonce), frame.body())
            .unwrap_or_else(|_| panic!("{} body is not decryptable", v.name));
        assert_eq!(pt.len(), v.payload_len as usize, "{}", v.name);
        let expected: Vec<u8> = (0..pt.len()).map(|i| (i % 251) as u8).collect();
        assert_eq!(pt, expected, "{} body pattern", v.name);
        checked += 1;
    }
    assert!(checked >= 7, "expected the sealed vectors, saw {checked}");
}

#[test]
fn aead_vectors_match_the_primitive() {
    let kat = load();
    for v in &kat.aead {
        let key = hex::decode(&v.key).unwrap();
        let nonce = hex::decode(&v.nonce).unwrap();
        let pt = hex::decode(&v.pt).unwrap();
        let ct = hex::decode(&v.ct).unwrap();
        assert_eq!(key.len(), 32, "{}", v.name);
        assert_eq!(nonce.len(), 12, "{}", v.name);
        assert_eq!(ct.len(), pt.len() + TAG_LEN, "{}", v.name);

        let cipher = ChaCha20Poly1305::new(Key::from_slice(&key));
        let got = cipher
            .encrypt(Nonce::from_slice(&nonce), pt.as_slice())
            .unwrap();
        assert_eq!(got, ct, "{} encrypt", v.name);
        let back = cipher
            .decrypt(Nonce::from_slice(&nonce), ct.as_slice())
            .unwrap();
        assert_eq!(back, pt, "{} decrypt", v.name);

        // Negative: one flipped tag byte must not authenticate.
        let mut broken = ct.clone();
        let last = broken.len() - 1;
        broken[last] ^= 0x01;
        assert!(
            cipher
                .decrypt(Nonce::from_slice(&nonce), broken.as_slice())
                .is_err(),
            "{} must reject a flipped tag",
            v.name
        );
    }
}

#[test]
fn hkdf_vectors_match_the_derivation() {
    let kat = load();
    for v in &kat.hkdf {
        assert_eq!(v.salt, "vade/pair/v1", "{}", v.name);
        assert_eq!(v.info, "", "{} — info is frozen empty", v.name);

        let core_pub = hex::decode(&v.core_pub).unwrap();
        assert_eq!(core_pub.len(), 32, "{}", v.name);
        assert_eq!(
            hex::encode(fingerprint_of(&core_pub).unwrap()),
            v.core_fp,
            "{} core_fp",
            v.name
        );
        assert_eq!(
            normalize_pair_code(&v.pair_code).unwrap(),
            v.normalized,
            "{} normalize",
            v.name
        );
        assert_eq!(
            hex::encode(derive_pair_psk(&v.pair_code, &core_pub).unwrap()),
            v.psk,
            "{} psk",
            v.name
        );
    }
    // The four spellings must derive identical PSKs (the point of NFKC).
    let first = &kat.hkdf[0].psk;
    for v in &kat.hkdf[1..] {
        assert_eq!(&v.psk, first, "{} must equal {}", v.name, kat.hkdf[0].name);
    }
}

#[test]
fn normalize_vectors_match() {
    let kat = load();
    for v in &kat.normalize {
        assert_eq!(
            normalize_pair_code(&v.input).unwrap(),
            v.expected,
            "{} ({:?})",
            v.name,
            v.input
        );
    }
}

#[test]
fn prologue_vectors_are_the_contract() {
    let text = std::fs::read_to_string(kat_path()).unwrap();
    let v: serde_json::Value = serde_json::from_str(&text).unwrap();
    let prologues = v["prologue"].as_array().unwrap();
    assert_eq!(prologues.len(), 2);
    assert_eq!(
        prologues[0]["bytes_hex"].as_str().unwrap(),
        hex::encode(b"vade/v1\x01")
    );
    assert_eq!(
        prologues[1]["bytes_hex"].as_str().unwrap(),
        hex::encode(b"vade/v1\x02")
    );
}
