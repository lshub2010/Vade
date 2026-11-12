//! Vade protocol v1 core — the single Rust implementation of the wire protocol.
//!
//! Scope (C1 task book §2): framing, Noise handshake, AEAD transport, replay
//! defence, error codes, KAT. **No IO, no business logic, no module knowledge.**
//! Bytes in, bytes out — that is what lets the same code back UniFFI (C0a) and
//! Wasm (C0b).
//!
//! ```text
//! Handshake (IK 1-RTT / XXpsk3 2-RTT)  ->  Transport (ChaCha20-Poly1305)
//!              |                                  |
//!         prologue "vade/v1"+role          frame header + AEAD body
//! ```
//!
//! # Layering
//!
//! * [`frame`] — the 20-byte header and strict, allocation-bounded decoding.
//! * [`handshake`] — thin, audited wrapper over `snow::HandshakeState`.
//! * [`transport`] — AEAD seal/open plus the `(generation, counter)` replay rule.
//! * [`identity`] — X25519 static keys; `Debug` is redacted.
//! * [`paircode`] — pair-code normalisation and PSK derivation (C-15 consumer).
//! * [`kat`] — typed loader for `protocol/v1/kat.json`.
//! * `ffi` — UniFFI surface, feature-gated, the only module allowed `unsafe`.
//!
//! # Non-negotiable rules encoded here
//!
//! 1. `snow` owns all cryptography. This crate never implements a primitive.
//! 2. Direction separation comes from the two Noise `Split` keys, not from the
//!    nonce; the frame `counter` only drives replay detection.
//! 3. Authentication failure is fatal: the receive nonce has already advanced
//!    (C0a finding 3), so the caller must drop the connection.
//! 4. The IK responder verifies the initiator's static key itself — `snow` does
//!    not compare it against `remote_public_key` (C0a finding 2).

#![deny(unsafe_code)]
#![warn(missing_debug_implementations)]

pub mod error;
pub mod frame;
pub mod handshake;
pub mod identity;
pub mod kat;
pub mod paircode;
pub mod transport;

#[cfg(feature = "uniffi")]
#[allow(unsafe_code)]
pub mod ffi;

// Must live in the crate root: it defines `UniFfiTag` and the exported symbols.
#[cfg(feature = "uniffi")]
uniffi::setup_scaffolding!("vade_protocol");

pub use error::{ErrorCode, ProtocolError, Result};
pub use frame::{
    Frame, FrameHeader, FrameType, Limits, DEFAULT_MAX_PAYLOAD, HEADER_LEN, MAX_HANDSHAKE_MSG,
    TAG_LEN, VERSION,
};
pub use handshake::{ConnectionRole, Handshake, Mode, Role, PROLOGUE_PREFIX};
pub use identity::Identity;
pub use paircode::{derive_pair_psk, normalize_pair_code};
pub use transport::{Direction, Transport, MAX_COUNTER};

/// Protocol identifier carried in the handshake prologue and in `module/register`.
pub const PROTOCOL: &str = "vade/v1";

/// Major protocol version, also the `ver` byte of every frame.
pub const PROTOCOL_VERSION: u8 = 1;

/// Crate version, e.g. `"0.1.0"`.
#[must_use]
pub fn version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// Major protocol version as an integer, for binding layers that cannot use `u8`.
#[must_use]
pub fn protocol_version() -> u32 {
    u32::from(PROTOCOL_VERSION)
}
