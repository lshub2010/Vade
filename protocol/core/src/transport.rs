//! Transport mode: AEAD seal/open, replay defence, generations.
//!
//! # Nonce
//!
//! `snow` owns the 96-bit AEAD nonce (4 zero bytes ‖ 8-byte little-endian
//! counter). The frame `counter` is a **separate, application-level** value used
//! only for replay detection; it restarts at `1` after a rekey even though the
//! AEAD nonce keeps advancing. `snow` never resets the nonce on rekey (Noise
//! §11.3), which is what we want — resetting it would risk nonce reuse.
//!
//! # Direction
//!
//! `Noise Split` yields two independent keys. [`Transport::open`] only ever uses
//! the receive cipher, so replaying our own outbound frame at `open` fails the
//! Poly1305 tag. [`Direction`] is a caller-side assertion of intent, not a nonce
//! input.
//!
//! # Replay rule
//!
//! Accepted iff `(generation, counter)` is strictly greater than the last
//! accepted pair, lexicographically, with `generation == last + 1` the only
//! legal jump. Anything else is fatal: there is no sliding window because a
//! WebSocket is reliable and ordered — out-of-order frames mean an attack or a
//! broken implementation.

use crate::error::{perr, pfatal, ErrorCode, ProtocolError, Result};
use crate::frame::{Frame, FrameHeader, FrameType, Limits, TAG_LEN, VERSION};

/// Highest counter we will use. Beyond this the caller must re-handshake
/// (FR-3.3: "计数耗尽即强制重握手"). The 1024-frame headroom keeps the last
/// frames of a connection usable while the caller notices.
pub const MAX_COUNTER: u64 = u64::MAX - 1024;

/// Which half of the session a call refers to, from *this* endpoint's view.
///
/// The c2m / m2c naming is a property of the Noise role, not of this enum: the
/// initiator's [`Direction::Outbound`] key is the initiator→responder key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "uniffi", derive(uniffi::Enum))]
pub enum Direction {
    /// Sealing. Only valid for [`Transport::seal`].
    Outbound,
    /// Opening. Only valid for [`Transport::open`].
    Inbound,
}

/// An established encrypted session. Holds both directions of one Noise
/// `TransportState`, plus the frame counters that back replay detection.
pub struct Transport {
    state: snow::TransportState,
    handshake_hash: Vec<u8>,
    is_initiator: bool,
    /// Generation of the next frame we send.
    send_generation: u32,
    /// Highest counter we have sent in `send_generation` (0 = none yet).
    send_counter: u64,
    /// Generation we expect from the peer.
    recv_generation: u32,
    /// Highest counter accepted in `recv_generation` (0 = none yet).
    recv_counter: u64,
    limits: Limits,
    rekeys: u64,
    /// Peer static public key, for whitelist/registration bookkeeping.
    remote_static: Option<Vec<u8>>,
}

impl Transport {
    pub(crate) fn new(
        state: snow::TransportState,
        handshake_hash: Vec<u8>,
        is_initiator: bool,
    ) -> Self {
        let remote_static = state.get_remote_static().map(<[u8]>::to_vec);
        Self {
            state,
            handshake_hash,
            is_initiator,
            send_generation: 1,
            send_counter: 0,
            recv_generation: 1,
            recv_counter: 0,
            limits: Limits::default(),
            rekeys: 0,
            remote_static,
        }
    }

    /// Peer static public key learned during the handshake. `XX` pairings use
    /// this to register the module; `IK` already knew it from the whitelist.
    #[must_use]
    pub fn remote_static(&self) -> Option<Vec<u8>> {
        self.remote_static.clone()
    }

    /// Override the payload cap (default 1 MiB).
    pub fn set_limits(&mut self, limits: Limits) {
        self.limits = limits;
    }

    #[must_use]
    pub fn limits(&self) -> Limits {
        self.limits
    }

    /// The effective plaintext cap of one frame: the manifest limit lowered to
    /// what a single Noise message can carry (65519 bytes).
    #[must_use]
    pub fn max_plaintext(&self) -> usize {
        std::cmp::min(self.limits.max_payload, crate::frame::MAX_NOISE_PAYLOAD) as usize
    }

    /// Noise `handshake_hash`: a session id both peers agree on, safe to log.
    #[must_use]
    pub fn handshake_hash(&self) -> &[u8] {
        &self.handshake_hash
    }

    #[must_use]
    pub fn is_initiator(&self) -> bool {
        self.is_initiator
    }

    /// Generation the next outbound frame will carry.
    #[must_use]
    pub fn send_generation(&self) -> u32 {
        self.send_generation
    }

    /// Generation the next inbound frame must carry.
    #[must_use]
    pub fn recv_generation(&self) -> u32 {
        self.recv_generation
    }

    #[must_use]
    pub fn send_counter(&self) -> u64 {
        self.send_counter
    }

    #[must_use]
    pub fn recv_counter(&self) -> u64 {
        self.recv_counter
    }

    /// How many rekeys this endpoint has performed (both directions).
    #[must_use]
    pub fn rekeys(&self) -> u64 {
        self.rekeys
    }

    /// Encrypt `plaintext` for the wire.
    ///
    /// `counter` must be exactly `send_counter + 1` — gaps are rejected so the
    /// peer can never see a hole it cannot interpret.
    pub fn seal(
        &mut self,
        dir: Direction,
        generation: u32,
        counter: u64,
        plaintext: &[u8],
    ) -> Result<Vec<u8>> {
        if dir != Direction::Outbound {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "seal() requires Direction::Outbound",
            ));
        }
        if generation != self.send_generation {
            return Err(perr(
                ErrorCode::InvalidArgument,
                format!(
                    "seal generation {generation} != current send generation {}",
                    self.send_generation
                ),
            ));
        }
        let next = self
            .send_counter
            .checked_add(1)
            .ok_or_else(|| perr(ErrorCode::Unsupported, "send counter exhausted"))?;
        if counter != next {
            return Err(perr(
                ErrorCode::InvalidArgument,
                format!("non-contiguous counter: expected {next}, got {counter}"),
            ));
        }
        if counter > MAX_COUNTER {
            return Err(perr(
                ErrorCode::Unsupported,
                format!("counter {counter} past {MAX_COUNTER}; re-handshake required"),
            ));
        }
        if plaintext.len() > self.max_plaintext() {
            return Err(perr(
                ErrorCode::PayloadTooLarge,
                format!(
                    "plaintext {} > per-frame cap {} (min of limits.max_payload {} and the Noise cap {})",
                    plaintext.len(),
                    self.max_plaintext(),
                    self.limits.max_payload,
                    crate::frame::MAX_NOISE_PAYLOAD
                ),
            ));
        }
        let mut out = vec![0u8; plaintext.len() + TAG_LEN];
        let n = self
            .state
            .write_message(plaintext, &mut out)
            .map_err(|e| ProtocolError::new(ErrorCode::Internal, format!("AEAD seal failed: {e}")))?;
        out.truncate(n);
        self.send_counter = counter;
        Ok(out)
    }

    /// Decrypt a ciphertext that came off the wire.
    ///
    /// The `(generation, counter)` check runs **before** the AEAD call: a
    /// replayed frame must not burn a receive nonce, and a version bump must
    /// never reach the cipher.
    pub fn open(
        &mut self,
        dir: Direction,
        generation: u32,
        counter: u64,
        ciphertext: &[u8],
    ) -> Result<Vec<u8>> {
        if dir != Direction::Inbound {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "open() requires Direction::Inbound",
            ));
        }
        self.check_inbound(generation, counter)?;
        if ciphertext.len() < TAG_LEN {
            return Err(perr(
                ErrorCode::InvalidArgument,
                format!("ciphertext {} shorter than tag {TAG_LEN}", ciphertext.len()),
            ));
        }
        if ciphertext.len() > self.max_plaintext() + TAG_LEN {
            return Err(perr(
                ErrorCode::PayloadTooLarge,
                format!("ciphertext {} over the per-frame cap", ciphertext.len()),
            ));
        }
        let mut out = vec![0u8; ciphertext.len() - TAG_LEN];
        let n = self.state.read_message(ciphertext, &mut out).map_err(|_| {
            // Verified in snow 0.10.0 `cipherstate.rs::decrypt_ad`: `self.n += 1`
            // only runs after a successful decrypt, so this failure does NOT
            // advance the receive nonce (the C0a "nonce advanced" observation
            // holds for the *handshake* state, not for transport mode here).
            // The error is fatal by policy anyway: a frame whose tag does not
            // verify means an on-path attacker or a broken peer, and the caller
            // has no way to tell which.
            pfatal(
                ErrorCode::PermissionDenied,
                "AEAD authentication failed; the link must be dropped",
            )
        })?;
        out.truncate(n);
        self.recv_counter = counter;
        Ok(out)
    }

    /// Seal a plaintext into a complete frame, using the internal counters.
    pub fn seal_frame(&mut self, typ: FrameType, plaintext: &[u8]) -> Result<Frame> {
        if typ.is_plaintext() {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "handshake frames are not sealed; use Handshake::write",
            ));
        }
        let generation = self.send_generation;
        let counter = self
            .send_counter
            .checked_add(1)
            .ok_or_else(|| perr(ErrorCode::Unsupported, "send counter exhausted"))?;
        let body = self.seal(Direction::Outbound, generation, counter, plaintext)?;
        Frame::from_body(typ, generation, counter, body)
    }

    /// Validate, decrypt and post-process one inbound frame.
    ///
    /// Handles `0x06 rekey` transparently: the caller gets the plaintext and
    /// [`Transport::recv_generation`] moves if the frame asked for it.
    pub fn open_frame(&mut self, frame: &Frame) -> Result<Vec<u8>> {
        let header = *frame.header();
        // Defence in depth: Frame::decode already rejected a wrong version, and
        // that rejection happens before any decryption by construction.
        if header.version != VERSION {
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                format!("frame version {} != {VERSION}", header.version),
            ));
        }
        header.check_limits(&self.limits)?;
        if header.typ.is_plaintext() {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "handshake frames are not opened by Transport",
            ));
        }
        let plaintext = self.open(
            Direction::Inbound,
            header.generation,
            header.counter,
            frame.body(),
        )?;
        if header.typ == FrameType::Rekey {
            self.apply_rekey(&plaintext)?;
        }
        Ok(plaintext)
    }

    /// Start a rekey: seal the `0x06` frame with the **old** key, then advance
    /// this endpoint's send generation.
    ///
    /// Only the sending direction moves; the peer's send generation is its own
    /// business (design §2.2.2 "方向性"). Nothing is returned about the peer —
    /// it will rekey its receive direction when it opens this frame.
    pub fn begin_rekey(&mut self) -> Result<Frame> {
        let new_generation = self
            .send_generation
            .checked_add(1)
            .ok_or_else(|| perr(ErrorCode::Unsupported, "generation exhausted"))?;
        let frame = self.seal_frame(FrameType::Rekey, &new_generation.to_be_bytes())?;
        self.rekey_outbound()?;
        Ok(frame)
    }

    /// Advance the send generation only. Used by [`Transport::begin_rekey`].
    fn rekey_outbound(&mut self) -> Result<u32> {
        self.state.rekey_outgoing();
        self.send_generation = self
            .send_generation
            .checked_add(1)
            .ok_or_else(|| perr(ErrorCode::Unsupported, "generation exhausted"))?;
        self.send_counter = 0;
        self.rekeys += 1;
        Ok(self.send_generation)
    }

    /// Advance the receive generation when a `0x06` frame asks for it.
    ///
    /// Idempotent on purpose: a duplicate or simultaneous rekey request
    /// (`new_generation <= recv_generation`) is ignored without disconnecting —
    /// both peers may hit the 24h threshold at the same moment. A jump
    /// (`new_generation > recv_generation + 1`) is fatal.
    fn apply_rekey(&mut self, plaintext: &[u8]) -> Result<()> {
        if plaintext.len() != 4 {
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                format!("rekey body must be 4 bytes, got {}", plaintext.len()),
            ));
        }
        let new_generation = u32::from_be_bytes([plaintext[0], plaintext[1], plaintext[2], plaintext[3]]);
        if new_generation <= self.recv_generation {
            // Stale or simultaneous: keep the current key, stay connected.
            return Ok(());
        }
        if new_generation != self.recv_generation + 1 {
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                format!(
                    "generation jump {} > {}",
                    new_generation,
                    self.recv_generation + 1
                ),
            ));
        }
        self.state.rekey_incoming();
        self.recv_generation = new_generation;
        self.recv_counter = 0;
        self.rekeys += 1;
        Ok(())
    }

    /// The replay / generation gate. No allocation, no crypto.
    fn check_inbound(&self, generation: u32, counter: u64) -> Result<()> {
        if generation < self.recv_generation {
            return Err(pfatal(
                ErrorCode::PermissionDenied,
                format!(
                    "generation rollback: frame gen {generation} < accepted {}",
                    self.recv_generation
                ),
            ));
        }
        if generation > self.recv_generation + 1 {
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                format!(
                    "generation jump: frame gen {generation} > {}",
                    self.recv_generation + 1
                ),
            ));
        }
        if generation == self.recv_generation && counter <= self.recv_counter {
            return Err(pfatal(
                ErrorCode::PermissionDenied,
                format!(
                    "replay: counter {counter} <= last accepted {}",
                    self.recv_counter
                ),
            ));
        }
        if counter > MAX_COUNTER {
            return Err(pfatal(
                ErrorCode::Unsupported,
                format!("counter {counter} past {MAX_COUNTER}; re-handshake required"),
            ));
        }
        Ok(())
    }
}

impl std::fmt::Debug for Transport {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Transport")
            .field("is_initiator", &self.is_initiator)
            .field("send_generation", &self.send_generation)
            .field("send_counter", &self.send_counter)
            .field("recv_generation", &self.recv_generation)
            .field("recv_counter", &self.recv_counter)
            .field("rekeys", &self.rekeys)
            .finish()
    }
}

/// Helper for callers that only have a header (streaming readers).
pub fn verify_header(header: &FrameHeader, limits: &Limits) -> Result<()> {
    if header.version != VERSION {
        return Err(pfatal(
            ErrorCode::ProtocolMismatch,
            format!("frame version {} != {VERSION}", header.version),
        ));
    }
    header.check_limits(limits)
}
