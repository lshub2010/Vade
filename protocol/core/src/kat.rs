//! Typed loader for `protocol/v1/kat.json` (contract **C-02**).
//!
//! Parsing only — reading the file is the caller's job, because the protocol
//! core does no IO. Every implementation (Rust, TS/Wasm, Python) loads the same
//! file and must reproduce the same bytes; that is what keeps the four bindings
//! from drifting apart.

use serde::Deserialize;

use crate::error::{perr, ErrorCode, Result};

/// Root of `kat.json`.
#[derive(Debug, Clone, Deserialize)]
pub struct Kat {
    /// File format version (not the protocol version).
    pub version: u32,
    /// Must be `"vade/v1"`.
    pub protocol: String,
    /// Frame codec vectors.
    pub frame: Vec<FrameVector>,
    /// AEAD primitive vectors.
    pub aead: Vec<AeadVector>,
    /// Pair-code PSK vectors.
    pub hkdf: Vec<HkdfVector>,
    /// Pair-code normalisation equivalences.
    #[serde(default)]
    pub normalize: Vec<NormalizeVector>,
}

/// One frame, encoded.
#[derive(Debug, Clone, Deserialize)]
pub struct FrameVector {
    pub name: String,
    /// `ver` byte, always 1.
    pub ver: u8,
    /// `type` byte.
    #[serde(rename = "type")]
    pub typ: u8,
    pub flags: u8,
    pub rsv: u8,
    pub generation: u32,
    pub counter: u64,
    /// Declared plaintext length.
    pub payload_len: u32,
    /// Full frame, hex.
    pub hex: String,
    /// Provenance: `generated` or `transcribed:<source>`.
    pub source: String,
}

/// One ChaCha20-Poly1305 vector. AAD is always empty (Noise does not use AAD).
#[derive(Debug, Clone, Deserialize)]
pub struct AeadVector {
    pub name: String,
    pub key: String,
    pub nonce: String,
    pub pt: String,
    pub ct: String,
    pub source: String,
}

/// One pair-code PSK vector.
#[derive(Debug, Clone, Deserialize)]
pub struct HkdfVector {
    pub name: String,
    pub pair_code: String,
    pub normalized: String,
    pub core_pub: String,
    pub core_fp: String,
    pub salt: String,
    pub info: String,
    pub psk: String,
    pub source: String,
}

/// One normalisation equivalence.
#[derive(Debug, Clone, Deserialize)]
pub struct NormalizeVector {
    pub name: String,
    pub input: String,
    pub expected: String,
}

impl Kat {
    /// Parse `kat.json` from a string.
    pub fn parse(json: &str) -> Result<Self> {
        let kat: Kat = serde_json::from_str(json)
            .map_err(|e| perr(ErrorCode::Internal, format!("kat.json is not valid: {e}")))?;
        if kat.version != 1 {
            return Err(perr(
                ErrorCode::ProtocolMismatch,
                format!("kat.json version {} != 1", kat.version),
            ));
        }
        if kat.protocol != crate::PROTOCOL {
            return Err(perr(
                ErrorCode::ProtocolMismatch,
                format!("kat.json protocol {:?} != {:?}", kat.protocol, crate::PROTOCOL),
            ));
        }
        if kat.frame.is_empty() || kat.aead.is_empty() || kat.hkdf.is_empty() {
            return Err(perr(
                ErrorCode::Internal,
                "kat.json is missing one of the frame/aead/hkdf sections",
            ));
        }
        Ok(kat)
    }

    /// Decode a frame vector's hex.
    pub fn frame_bytes(v: &FrameVector) -> Result<Vec<u8>> {
        hex::decode(&v.hex)
            .map_err(|e| perr(ErrorCode::Internal, format!("frame {}: bad hex: {e}", v.name)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_a_foreign_protocol_id() {
        let json = r#"{"version":1,"protocol":"vade/v2","frame":[],"aead":[],"hkdf":[]}"#;
        assert_eq!(
            Kat::parse(json).unwrap_err().code(),
            ErrorCode::ProtocolMismatch
        );
    }
}
