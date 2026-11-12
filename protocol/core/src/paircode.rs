//! Pair code normalisation and PSK derivation (contract **C-15**).
//!
//! `PSK = HKDF-SHA256(ikm = utf8(normalize(pair_code)) ‖ core_pub_fp,
//!                    salt = "vade/pair/v1", info = "", L = 32)`
//! where `core_pub_fp = BLAKE2s-256(core_static_public_key)` (32 raw bytes,
//! **not** hex).
//!
//! `normalize()` per C-15 / FR-4.9: **NFKC → drop `-`, spaces and the middle
//! dots → upper-case**. NFKC is what makes a full-width `Ａ７Ｋ２Ｍ９` equal to
//! `A7K2M9`; without it a phone IME silently produces a different PSK.
//!
//! `info` is empty because neither `design.md` §2.2 nor FR-4.9 defines one.
//! It is frozen as empty — adding an `info` later is a breaking protocol change
//! and must go through an RFC, not a code edit.
//!
//! This module is the executable copy of C-15's rule; `protocol/v1/kat.json`
//! carries the vectors other implementations (TS/Wasm, Python) must match.

use blake2::{Blake2s256, Digest};
use hkdf::Hkdf;
use sha2::Sha256;
use unicode_normalization::UnicodeNormalization;

use crate::error::{perr, ErrorCode, Result};
use crate::identity::KEY_LEN;

/// HKDF salt. Frozen; changing it changes every PSK.
pub const PAIR_PSK_SALT: &[u8] = b"vade/pair/v1";
/// HKDF info. Frozen as empty (see module docs).
pub const PAIR_PSK_INFO: &[u8] = b"";
/// Number of significant characters: 2 hanzi + 6 base32.
pub const PAIR_CODE_CHARS: usize = 8;
/// Base32 alphabet with `I`, `O`, `0`, `1` removed.
pub const BASE32_ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";

/// Characters dropped before the case fold. NFKC first turns `－` (U+FF0D) into
/// `-`, so the ASCII hyphen covers the full-width form.
const SEPARATORS: [char; 6] = ['-', ' ', '\t', '\u{00B7}', '\u{30FB}', '\u{3000}'];

/// Normalise a pair code: NFKC, drop separators, upper-case.
///
/// Does **not** validate the alphabet — that is [`derive_pair_psk`]'s job, so
/// this function stays a pure, easily-tested string transform.
pub fn normalize_pair_code(input: &str) -> Result<String> {
    if input.is_empty() {
        return Err(perr(ErrorCode::InvalidArgument, "pair code is empty"));
    }
    let nfkc: String = input.nfkc().collect();
    let stripped: String = nfkc.chars().filter(|c| !SEPARATORS.contains(c)).collect();
    if stripped.is_empty() {
        return Err(perr(
            ErrorCode::InvalidArgument,
            "pair code is empty after normalisation",
        ));
    }
    // char::to_uppercase is locale-independent: no Turkish-i surprise.
    Ok(stripped.chars().flat_map(char::to_uppercase).collect())
}

/// Validate the normalised shape: 8 characters, last 6 in the base32 alphabet.
///
/// The 2-character hanzi prefix is **not** checked against C-15's 256-entry
/// table here — that table is not in this crate (see RESULT.md, 已知限制).
pub fn validate_normalized(code: &str) -> Result<()> {
    let chars: Vec<char> = code.chars().collect();
    if chars.len() != PAIR_CODE_CHARS {
        return Err(perr(
            ErrorCode::InvalidArgument,
            format!(
                "pair code must be {PAIR_CODE_CHARS} characters after normalisation, got {}",
                chars.len()
            ),
        ));
    }
    for (i, c) in chars[PAIR_CODE_CHARS - 6..].iter().enumerate() {
        if !c.is_ascii() || !BASE32_ALPHABET.contains(&(*c as u8)) {
            return Err(perr(
                ErrorCode::InvalidArgument,
                format!("pair code character {} is not in the base32 alphabet", i + 3),
            ));
        }
    }
    Ok(())
}

/// Normalise **and** validate, then derive the 32-byte PSK.
pub fn derive_pair_psk(pair_code: &str, core_public_key: &[u8]) -> Result<[u8; 32]> {
    if core_public_key.len() != KEY_LEN {
        return Err(perr(
            ErrorCode::InvalidArgument,
            format!(
                "core public key must be {KEY_LEN} bytes, got {}",
                core_public_key.len()
            ),
        ));
    }
    let normalized = normalize_pair_code(pair_code)?;
    validate_normalized(&normalized)?;

    let mut hasher = Blake2s256::new();
    hasher.update(core_public_key);
    let core_fp = hasher.finalize();

    let mut ikm = Vec::with_capacity(normalized.len() + core_fp.len());
    ikm.extend_from_slice(normalized.as_bytes());
    ikm.extend_from_slice(&core_fp);

    let hk = Hkdf::<Sha256>::new(Some(PAIR_PSK_SALT), &ikm);
    let mut okm = [0u8; 32];
    hk.expand(PAIR_PSK_INFO, &mut okm)
        .map_err(|_| perr(ErrorCode::Internal, "HKDF expand failed"))?;
    Ok(okm)
}

/// Hex form of [`derive_pair_psk`], for logs and audit records.
pub fn derive_pair_psk_hex(pair_code: &str, core_public_key: &[u8]) -> Result<String> {
    Ok(hex::encode(derive_pair_psk(pair_code, core_public_key)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn separators_and_case_do_not_change_the_code() {
        let want = "青松A7K2M9";
        for input in [
            "青松-A7K2M9",
            "青松 a7k2m9",
            "青松·A7K2M9",
            "青松－Ａ７Ｋ２Ｍ９",
            " 青松-A7K2M9 ",
        ] {
            assert_eq!(normalize_pair_code(input).unwrap(), want, "input {input:?}");
        }
    }

    #[test]
    fn equivalent_inputs_derive_the_same_psk() {
        let core = [0x11u8; 32];
        let a = derive_pair_psk("青松-A7K2M9", &core).unwrap();
        let b = derive_pair_psk("青松－Ａ７Ｋ２Ｍ９", &core).unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn wrong_shape_is_rejected() {
        let core = [0x11u8; 32];
        assert_eq!(
            derive_pair_psk("青松-A7K2M", &core).unwrap_err().code(),
            ErrorCode::InvalidArgument
        );
        // `I` and `0` are not in the alphabet.
        assert_eq!(
            derive_pair_psk("青松-A0K2M9", &core).unwrap_err().code(),
            ErrorCode::InvalidArgument
        );
    }
}
