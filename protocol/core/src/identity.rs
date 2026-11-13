//! X25519 static identity.
//!
//! The private key never appears in `Debug`, never appears in an error message,
//! and is zeroised on drop. Only [`Identity::to_secret_bytes`] hands it out, and
//! that is documented for the 0600 seed file exclusively (FR-3.8: core.key is a
//! plaintext seed on disk; the protocol core does not pretend otherwise).

use std::fmt;

use blake2::{Blake2s256, Digest};
use snow::params::NoiseParams;
use snow::Builder;
use x25519_dalek::{PublicKey, StaticSecret};
use zeroize::Zeroize;

use crate::error::{perr, ErrorCode, ProtocolError, Result};

/// X25519 key length.
pub const KEY_LEN: usize = 32;

/// Noise parameter set used only to drive `snow`'s key generation, so the RNG
/// source is identical to the one the handshake uses.
const KEYGEN_PARAMS: &str = "Noise_IK_25519_ChaChaPoly_BLAKE2s";

/// A long-term X25519 static key pair.
///
/// The static key participates in authentication DH only — it never encrypts
/// payload bytes directly (FR-3.4).
#[derive(Clone)]
pub struct Identity {
    /// The only copy we own; wiped by `Drop`.
    secret_bytes: [u8; KEY_LEN],
    public: [u8; KEY_LEN],
}

impl Identity {
    /// Generate a fresh key pair from the OS CSPRNG.
    pub fn generate() -> Result<Self> {
        let params: NoiseParams = KEYGEN_PARAMS
            .parse()
            .map_err(|_| ProtocolError::new(ErrorCode::Internal, "built-in Noise params invalid"))?;
        let keypair = Builder::new(params)
            .generate_keypair()
            .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("keygen failed: {e}")))?;
        let mut private = keypair.private;
        let id = Self::from_secret_bytes(&private)?;
        private.zeroize();
        Ok(id)
    }

    /// Import a 32-byte X25519 secret. The public key is derived, not trusted.
    pub fn from_secret_bytes(bytes: &[u8]) -> Result<Self> {
        if bytes.len() != KEY_LEN {
            return Err(perr(
                ErrorCode::InvalidArgument,
                format!("X25519 secret must be {KEY_LEN} bytes, got {}", bytes.len()),
            ));
        }
        let mut secret_bytes = [0u8; KEY_LEN];
        secret_bytes.copy_from_slice(bytes);
        // `StaticSecret` is a temporary: the public key is derived with the same
        // clamped-scalar maths snow will use during the handshake.
        let secret = StaticSecret::from(secret_bytes);
        let public = PublicKey::from(&secret).to_bytes();
        Ok(Self {
            secret_bytes,
            public,
        })
    }

    /// Import from hex (the 0600 seed file format).
    pub fn from_secret_hex(s: &str) -> Result<Self> {
        let raw = hex::decode(s.trim())
            .map_err(|e| perr(ErrorCode::InvalidArgument, format!("secret is not hex: {e}")))?;
        Self::from_secret_bytes(&raw)
    }

    /// **Seed-file only.** Returns the raw private key. Callers must write it to
    /// a 0600 file, never to a log, an audit record, or the wire.
    #[must_use]
    pub fn to_secret_bytes(&self) -> Vec<u8> {
        self.secret_bytes.to_vec()
    }

    /// **Seed-file only.** Hex form of [`Identity::to_secret_bytes`].
    #[must_use]
    pub fn to_secret_hex(&self) -> String {
        hex::encode(self.secret_bytes)
    }

    #[must_use]
    pub fn public_key(&self) -> Vec<u8> {
        self.public.to_vec()
    }

    #[must_use]
    pub fn public_key_array(&self) -> [u8; KEY_LEN] {
        self.public
    }

    #[must_use]
    pub fn public_key_hex(&self) -> String {
        hex::encode(self.public)
    }

    /// `BLAKE2s-256(public_key)` — the whitelist key (`module_id → 公钥指纹`, FR-4.1)
    /// and the `core_pub_fp` input of the pair-code PSK (C-15).
    #[must_use]
    pub fn fingerprint(&self) -> Vec<u8> {
        let mut hasher = Blake2s256::new();
        hasher.update(self.public);
        hasher.finalize().to_vec()
    }

    #[must_use]
    pub fn fingerprint_hex(&self) -> String {
        hex::encode(self.fingerprint())
    }
}

impl fmt::Debug for Identity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Identity")
            .field("public", &self.public_key_hex())
            .field("secret", &"[redacted]")
            .finish()
    }
}

impl Drop for Identity {
    fn drop(&mut self) {
        self.secret_bytes.zeroize();
    }
}

/// `BLAKE2s-256` fingerprint of a 32-byte public key.
pub fn fingerprint_of(public_key: &[u8]) -> Result<Vec<u8>> {
    if public_key.len() != KEY_LEN {
        return Err(perr(
            ErrorCode::InvalidArgument,
            format!("public key must be {KEY_LEN} bytes, got {}", public_key.len()),
        ));
    }
    let mut hasher = Blake2s256::new();
    hasher.update(public_key);
    Ok(hasher.finalize().to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_key_is_derived_from_the_secret() {
        let secret = [7u8; KEY_LEN];
        let a = Identity::from_secret_bytes(&secret).unwrap();
        let b = Identity::from_secret_hex(&hex::encode(secret)).unwrap();
        assert_eq!(a.public_key(), b.public_key());
        assert_eq!(a.fingerprint_hex(), b.fingerprint_hex());
        assert_eq!(a.fingerprint().len(), 32);
    }

    #[test]
    fn debug_never_prints_the_secret() {
        let id = Identity::from_secret_bytes(&[9u8; KEY_LEN]).unwrap();
        let shown = format!("{id:?}");
        assert!(shown.contains("[redacted]"), "{shown}");
        assert!(!shown.contains(&hex::encode([9u8; KEY_LEN])), "{shown}");
    }

    #[test]
    fn wrong_length_is_rejected() {
        assert_eq!(
            Identity::from_secret_bytes(&[0u8; 31]).unwrap_err().code(),
            ErrorCode::InvalidArgument
        );
    }
}
