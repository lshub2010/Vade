//! Noise handshake wrapper: `IK` for normal connections, `XXpsk3` for pairing.
//!
//! `snow` owns the state machine. This module adds the four things the spec
//! requires and `snow` does not do for us:
//!
//! 1. **Prologue binding** — `b"vade/v1" || connection_role_byte`. Both peers
//!    must pass the *same* byte (the role of the party that opened the
//!    connection, C0a finding 1). A mismatch changes the transcript hash and the
//!    handshake fails; that is the first anti-cross-protocol check.
//! 2. **Whitelist enforcement** — the IK responder learns the initiator's static
//!    key from msg1 and `snow` accepts *any* key (C0a finding 2). We compare
//!    `get_remote_static()` against the expected key ourselves, right after the
//!    message that reveals it, and before writing the next message.
//! 3. **No downgrade** — `IK` without `remote_pub` is `INVALID_ARGUMENT`; it
//!    never silently becomes `XX`.
//! 4. **Message cap** — handshake messages > 65535 bytes are refused with
//!    `PAYLOAD_TOO_LARGE` before `snow` ever sees them.
//!
//! The handshake is synchronous and pure: it takes bytes, returns bytes. IO
//! belongs to the SDK (C0b).

use snow::params::NoiseParams;
use snow::{Builder, HandshakeState};

use crate::error::{perr, pfatal, ErrorCode, ProtocolError, Result};
use crate::frame::MAX_HANDSHAKE_MSG;
use crate::identity::{Identity, KEY_LEN};
use crate::transport::Transport;

/// Prologue prefix; the connection-role byte is appended.
pub const PROLOGUE_PREFIX: &[u8] = b"vade/v1";

/// Parameter set for reconnects from an already-paired peer (1-RTT).
pub const NOISE_IK: &str = "Noise_IK_25519_ChaChaPoly_BLAKE2s";
/// Parameter set for first pairing (2-RTT). PSK is mixed at index 3.
pub const NOISE_XXPSK3: &str = "Noise_XXpsk3_25519_ChaChaPoly_BLAKE2s";

/// Local Noise role.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "uniffi", derive(uniffi::Enum))]
pub enum Role {
    /// Writes msg1, 1-RTT with `IK`.
    Initiator,
    /// Reads msg1 first.
    Responder,
}

/// Handshake pattern.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "uniffi", derive(uniffi::Enum))]
pub enum Mode {
    /// `Noise_IK_25519_ChaChaPoly_BLAKE2s` — both statics known up front.
    Ik,
    /// `Noise_XXpsk3_25519_ChaChaPoly_BLAKE2s` — first pairing, pair-code PSK.
    XxPsk,
}

/// Who opened the connection. **Both peers must pass the same value.**
///
/// This is the byte appended to the prologue. It describes the connection, not
/// the caller: a core that accepts an inbound connection is the *responder*
/// while still passing [`ConnectionRole::ModuleInitiated`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "uniffi", derive(uniffi::Enum))]
pub enum ConnectionRole {
    /// The core opened the connection (core = Noise initiator). Byte `1`.
    CoreInitiated,
    /// The module opened the connection (module = Noise initiator). Byte `2`.
    ModuleInitiated,
}

impl ConnectionRole {
    #[must_use]
    pub fn as_byte(self) -> u8 {
        match self {
            ConnectionRole::CoreInitiated => 1,
            ConnectionRole::ModuleInitiated => 2,
        }
    }
}

/// `b"vade/v1" || role_byte`, identical on both ends of one connection.
#[must_use]
pub fn prologue(connection: ConnectionRole) -> Vec<u8> {
    let mut out = Vec::with_capacity(PROLOGUE_PREFIX.len() + 1);
    out.extend_from_slice(PROLOGUE_PREFIX);
    out.push(connection.as_byte());
    out
}

/// A live handshake. Holds `snow`'s state machine; no IO, no lifetimes escaping.
pub struct Handshake {
    state: HandshakeState,
    mode: Mode,
    role: Role,
    /// Static key the peer must prove possession of.
    expected_remote: Option<[u8; KEY_LEN]>,
    /// Set once `expected_remote` has actually been compared.
    remote_verified: bool,
    finished: bool,
}

impl Handshake {
    /// Start a handshake.
    ///
    /// * `mode` / `role` — the Noise pattern and this side's role.
    /// * `connection` — who dialled; both peers must agree (prologue byte).
    /// * `remote_pub` — **required for `IK`** (no downgrade to `XX`); for
    ///   `XXpsk3` it is optional pinning, checked as soon as the peer's static
    ///   key becomes visible.
    /// * `psk` — required for `XXpsk3` (32 bytes, from
    ///   [`crate::paircode::derive_pair_psk`]); ignored for `IK`.
    pub fn new(
        mode: Mode,
        role: Role,
        connection: ConnectionRole,
        local: &Identity,
        remote_pub: Option<&[u8]>,
        psk: Option<&[u8]>,
    ) -> Result<Self> {
        let expected_remote = match (mode, remote_pub) {
            (Mode::Ik, None) => {
                return Err(perr(
                    ErrorCode::InvalidArgument,
                    "IK requires the peer static public key; refusing to fall back to XX",
                ))
            }
            (_, Some(raw)) if raw.len() != KEY_LEN => {
                return Err(perr(
                    ErrorCode::InvalidArgument,
                    format!("peer static key must be {KEY_LEN} bytes, got {}", raw.len()),
                ))
            }
            (_, Some(raw)) => {
                let mut k = [0u8; KEY_LEN];
                k.copy_from_slice(raw);
                Some(k)
            }
            (Mode::XxPsk, None) => None,
        };

        let psk = match (mode, psk) {
            (Mode::XxPsk, None) => {
                return Err(perr(
                    ErrorCode::InvalidArgument,
                    "XXpsk3 requires a 32-byte PSK derived from the pair code",
                ))
            }
            (_, Some(k)) if mode == Mode::XxPsk && k.len() != 32 => {
                return Err(perr(
                    ErrorCode::InvalidArgument,
                    format!("PSK must be 32 bytes, got {}", k.len()),
                ))
            }
            (Mode::XxPsk, Some(k)) => Some(k),
            (Mode::Ik, _) => None,
        };

        let params: NoiseParams = params_str(mode)
            .parse()
            .map_err(|_| ProtocolError::new(ErrorCode::Internal, "built-in Noise params invalid"))?;
        let prologue = prologue(connection);
        let local_secret = local.to_secret_bytes();
        // Hoisted so the builder's borrow outlives this `if let`.
        let remote_key = expected_remote;

        let mut builder = Builder::new(params)
            .local_private_key(&local_secret)
            .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("local key rejected: {e}")))?
            .prologue(&prologue)
            .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("prologue rejected: {e}")))?;

        if let Some(k) = remote_key.as_ref() {
            builder = builder
                .remote_public_key(k)
                .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("remote key rejected: {e}")))?;
        }

        let psk_arr: Option<[u8; 32]> = match psk {
            Some(k) => {
                let mut a = [0u8; 32];
                a.copy_from_slice(k);
                Some(a)
            }
            None => None,
        };

        let state = match (role, psk_arr) {
            (Role::Initiator, Some(a)) => builder
                .psk(3, &a)
                .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("psk rejected: {e}")))?
                .build_initiator(),
            (Role::Responder, Some(a)) => builder
                .psk(3, &a)
                .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("psk rejected: {e}")))?
                .build_responder(),
            (Role::Initiator, None) => builder.build_initiator(),
            (Role::Responder, None) => builder.build_responder(),
        }
        .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("handshake init failed: {e}")))?;

        Ok(Self {
            state,
            mode,
            role,
            expected_remote,
            remote_verified: expected_remote.is_none(),
            finished: false,
        })
    }

    /// Write the next handshake message, with an optional payload.
    pub fn write(&mut self, payload: &[u8]) -> Result<Vec<u8>> {
        if self.finished {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "handshake already finished",
            ));
        }
        if payload.len() > MAX_HANDSHAKE_MSG {
            return Err(perr(
                ErrorCode::PayloadTooLarge,
                format!("handshake payload {} > {MAX_HANDSHAKE_MSG}", payload.len()),
            ));
        }
        let mut buf = vec![0u8; MAX_HANDSHAKE_MSG];
        let n = self.state.write_message(payload, &mut buf).map_err(|e| {
            ProtocolError::new(ErrorCode::Internal, format!("handshake write failed: {e}"))
        })?;
        buf.truncate(n);
        if self.state.is_handshake_finished() {
            self.finished = true;
        }
        Ok(buf)
    }

    /// Read the next handshake message. Any failure is an authentication
    /// failure: the peer is not who it claims to be.
    pub fn read(&mut self, msg: &[u8]) -> Result<Vec<u8>> {
        if self.finished {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "handshake already finished",
            ));
        }
        if msg.len() > MAX_HANDSHAKE_MSG {
            return Err(pfatal(
                ErrorCode::PayloadTooLarge,
                format!("handshake message {} > {MAX_HANDSHAKE_MSG}", msg.len()),
            ));
        }
        let mut buf = vec![0u8; msg.len().max(1)];
        let n = self.state.read_message(msg, &mut buf).map_err(|_| {
            // No detail: a distinguishable message is an oracle.
            pfatal(
                ErrorCode::PermissionDenied,
                "handshake authentication failed",
            )
        })?;
        buf.truncate(n);
        // The peer's static key becomes visible during the handshake; compare it
        // now, before we emit the next message and before anyone treats the peer
        // as authenticated.
        self.verify_remote_static()?;
        if self.state.is_handshake_finished() {
            self.finished = true;
        }
        Ok(buf)
    }

    /// True once the pattern has completed on this side.
    #[must_use]
    pub fn is_finished(&self) -> bool {
        self.finished
    }

    #[must_use]
    pub fn mode(&self) -> Mode {
        self.mode
    }

    #[must_use]
    pub fn role(&self) -> Role {
        self.role
    }

    /// Peer static public key, once the pattern has revealed it.
    #[must_use]
    pub fn remote_static(&self) -> Option<Vec<u8>> {
        self.state.get_remote_static().map(<[u8]>::to_vec)
    }

    /// `handshake_hash` (`h`) — a stable session id both peers agree on.
    #[must_use]
    pub fn handshake_hash(&self) -> Vec<u8> {
        self.state.get_handshake_hash().to_vec()
    }

    /// Whether the peer's static key has been compared against the expected one.
    #[must_use]
    pub fn remote_verified(&self) -> bool {
        self.remote_verified
    }

    /// Convert to transport mode. Fails if the pattern has not completed.
    pub fn into_transport(self) -> Result<Transport> {
        if !self.finished {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "handshake is not finished; refusing to enter transport mode",
            ));
        }
        // Last line of defence: never hand an unverified peer to the transport.
        let expected = self.expected_remote;
        let actual = self.state.get_remote_static().map(<[u8]>::to_vec);
        if let Some(exp) = expected {
            match actual {
                Some(a) if a == exp => {}
                _ => {
                    return Err(pfatal(
                        ErrorCode::PermissionDenied,
                        "peer static key is not the whitelisted key",
                    ))
                }
            }
        }
        let hash = self.state.get_handshake_hash().to_vec();
        let is_initiator = self.role == Role::Initiator;
        let state = self
            .state
            .into_transport_mode()
            .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("transport mode: {e}")))?;
        Ok(Transport::new(state, hash, is_initiator))
    }

    fn verify_remote_static(&mut self) -> Result<()> {
        let Some(expected) = self.expected_remote else {
            return Ok(());
        };
        let Some(actual) = self.state.get_remote_static() else {
            // Not revealed yet (e.g. XX initiator before msg2).
            return Ok(());
        };
        if actual != expected {
            return Err(pfatal(
                ErrorCode::PermissionDenied,
                "peer static key is not the whitelisted key",
            ));
        }
        self.remote_verified = true;
        Ok(())
    }
}

impl std::fmt::Debug for Handshake {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Handshake")
            .field("mode", &self.mode)
            .field("role", &self.role)
            .field("finished", &self.finished)
            .field("remote_verified", &self.remote_verified)
            .finish()
    }
}

fn params_str(mode: Mode) -> &'static str {
    match mode {
        Mode::Ik => NOISE_IK,
        Mode::XxPsk => NOISE_XXPSK3,
    }
}
