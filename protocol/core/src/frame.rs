//! Wire framing: the 20-byte header, strict decoding, bounded allocation.
//!
//! ```text
//!  0        1        2        3
//! +--------+--------+--------+--------+
//! | ver(1) | type(1)| flags(1)| rsv(1) |   ver   = 1            (PROTOCOL_MISMATCH otherwise)
//! +--------+--------+--------+--------+   type  = 1..=6
//! | payload_len (4, BE)                |   payload_len = PLAINTEXT length
//! +------------------------------------+   flags = 0 (reserved)
//! | generation (4, BE)                 |   rsv   = 0 (reserved)
//! +------------------------------------+
//! | counter (8, BE)                    |
//! +------------------------------------+
//! | body: payload_len bytes (handshake) or payload_len + 16 bytes (AEAD) |
//! +---------------------------------------------------------------------+
//! ```
//!
//! `HEADER_LEN` is **20**, not 16. The C1 task book (T2) says
//! "帧总长 = 16 + payload_len + 16" while its own field list is
//! `1+1+1+1+4+4+8 = 20`; `design.md` §2.1 draws all seven fields. C-01 freezes
//! the field list (20) and records the arithmetic as erratum **E1** — a
//! 16-byte header cannot carry `flags`, `rsv` and `payload_len`.
//!
//! Decoding is **strict**:
//! * `ver != 1` → `PROTOCOL_MISMATCH` before any buffer is sized or any crypto runs.
//! * `payload_len > limits.max_payload` → `PAYLOAD_TOO_LARGE` **without
//!   allocating** `payload_len` bytes (anti memory-amplification).
//! * declared length != actual bytes → `INVALID_ARGUMENT`; no lenient "parse
//!   what you got" mode, ever.

use crate::error::{perr, pfatal, ErrorCode, ProtocolError, Result};

/// Protocol major version, the `ver` byte.
pub const VERSION: u8 = 1;
/// Header size: 4 one-byte fields + u32 + u32 + u64.
pub const HEADER_LEN: usize = 20;
/// Poly1305 tag length appended by ChaCha20-Poly1305.
pub const TAG_LEN: usize = 16;
/// Noise caps a message at 65535 bytes; handshake frames inherit that cap.
pub const MAX_HANDSHAKE_MSG: usize = 65535;
/// **Physical** per-frame plaintext cap: `MAXMSGLEN - TAG_LEN`.
///
/// `Noise_*_ChaChaPoly_BLAKE2s` cannot carry more than 65535 bytes per message,
/// and the tag eats 16 of them. `limits.max_payload` (default 1 MiB) is the
/// *logical* cap from the manifest; a single frame can never exceed this one.
/// Payloads above it must be chunked above the protocol core.
pub const MAX_NOISE_PAYLOAD: u32 = (MAX_HANDSHAKE_MSG as u32) - (TAG_LEN as u32);
/// Default `limits.max_payload` (1 MiB).
pub const DEFAULT_MAX_PAYLOAD: u32 = 1 << 20;

/// Frame types. `0x06 rekey` is the control frame that starts a new generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[cfg_attr(feature = "uniffi", derive(uniffi::Enum))]
pub enum FrameType {
    /// `0x01` — carries a raw Noise handshake message, plaintext, counter = 0.
    Handshake,
    /// `0x02` — JSON-RPC request/response.
    Rpc,
    /// `0x03` — core → module event.
    Event,
    /// `0x04` — keepalive.
    Heartbeat,
    /// `0x05` — orderly close with a `{code,message}` body.
    Close,
    /// `0x06` — rekey; body is `{new_generation: u32 BE}`, sealed with the old key.
    Rekey,
}

impl FrameType {
    #[must_use]
    pub fn as_u8(self) -> u8 {
        match self {
            FrameType::Handshake => 0x01,
            FrameType::Rpc => 0x02,
            FrameType::Event => 0x03,
            FrameType::Heartbeat => 0x04,
            FrameType::Close => 0x05,
            FrameType::Rekey => 0x06,
        }
    }

    /// Unknown type bytes are a dialect mismatch, not a programming error.
    pub fn from_u8(b: u8) -> Result<Self> {
        Ok(match b {
            0x01 => FrameType::Handshake,
            0x02 => FrameType::Rpc,
            0x03 => FrameType::Event,
            0x04 => FrameType::Heartbeat,
            0x05 => FrameType::Close,
            0x06 => FrameType::Rekey,
            other => {
                return Err(pfatal(
                    ErrorCode::ProtocolMismatch,
                    format!("unknown frame type 0x{other:02x}"),
                ))
            }
        })
    }

    /// Handshake frames carry plaintext; every other type carries AEAD output.
    #[must_use]
    pub fn is_plaintext(self) -> bool {
        matches!(self, FrameType::Handshake)
    }
}

/// Receiver-side size caps. Passed to [`Frame::decode`] so a header can never
/// cause a large allocation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// Maximum declared plaintext length of one frame.
    pub max_payload: u32,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_payload: DEFAULT_MAX_PAYLOAD,
        }
    }
}

impl Limits {
    #[must_use]
    pub fn new(max_payload: u32) -> Self {
        Self { max_payload }
    }
}

/// The fixed part of a frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FrameHeader {
    /// Protocol major version. Always [`VERSION`] on a valid frame.
    pub version: u8,
    /// Frame type byte.
    pub typ: FrameType,
    /// Reserved flags. Frozen to `0` in v1; a non-zero value is a mismatch.
    pub flags: u8,
    /// Reserved byte. Frozen to `0` in v1.
    pub reserved: u8,
    /// **Plaintext** length. Ciphertext is `payload_len + TAG_LEN`.
    pub payload_len: u32,
    /// Key generation. `1` after the handshake, `+1` per rekey.
    pub generation: u32,
    /// Per-direction frame counter. Starts at `1`, contiguous, never reused.
    pub counter: u64,
}

impl FrameHeader {
    #[must_use]
    pub fn new(typ: FrameType, generation: u32, counter: u64, payload_len: u32) -> Self {
        Self {
            version: VERSION,
            typ,
            flags: 0,
            reserved: 0,
            payload_len,
            generation,
            counter,
        }
    }

    /// Serialise the header. Cannot fail: all invariants live in constructors
    /// and [`FrameHeader::peek`].
    #[must_use]
    pub fn encode(&self) -> [u8; HEADER_LEN] {
        let mut out = [0u8; HEADER_LEN];
        out[0] = self.version;
        out[1] = self.typ.as_u8();
        out[2] = self.flags;
        out[3] = self.reserved;
        out[4..8].copy_from_slice(&self.payload_len.to_be_bytes());
        out[8..12].copy_from_slice(&self.generation.to_be_bytes());
        out[12..20].copy_from_slice(&self.counter.to_be_bytes());
        out
    }

    /// Parse a header from the first [`HEADER_LEN`] bytes of a buffer.
    ///
    /// This is the only entry point that sees attacker-controlled bytes, so all
    /// protocol-level rejection happens here — before any buffer is sized and
    /// before any AEAD call. Extra bytes after the header are ignored, which is
    /// what a streaming reader wants.
    pub fn peek(prefix: &[u8]) -> Result<Self> {
        if prefix.len() < HEADER_LEN {
            return Err(perr(
                ErrorCode::InvalidArgument,
                format!(
                    "truncated frame header: got {} bytes, need {HEADER_LEN}",
                    prefix.len()
                ),
            ));
        }
        let version = prefix[0];
        if version != VERSION {
            // Rejected before decryption, always. Never negotiate, never downgrade.
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                format!("unsupported protocol version {version}, expected {VERSION}"),
            ));
        }
        let typ = FrameType::from_u8(prefix[1])?;
        let flags = prefix[2];
        let reserved = prefix[3];
        if flags != 0 {
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                format!("reserved flags must be 0, got 0x{flags:02x}"),
            ));
        }
        if reserved != 0 {
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                format!("reserved byte must be 0, got 0x{reserved:02x}"),
            ));
        }
        let payload_len = u32::from_be_bytes([prefix[4], prefix[5], prefix[6], prefix[7]]);
        let generation = u32::from_be_bytes([prefix[8], prefix[9], prefix[10], prefix[11]]);
        let counter = u64::from_be_bytes([
            prefix[12], prefix[13], prefix[14], prefix[15], prefix[16], prefix[17], prefix[18],
            prefix[19],
        ]);
        if typ.is_plaintext() {
            // Handshake frames run before any key exists: generation 0, counter 0.
            if counter != 0 {
                return Err(pfatal(
                    ErrorCode::ProtocolMismatch,
                    "handshake frames must use counter 0",
                ));
            }
            if generation != 0 {
                return Err(pfatal(
                    ErrorCode::ProtocolMismatch,
                    "handshake frames must use generation 0",
                ));
            }
        } else if counter == 0 {
            // Encrypted counters start at 1; (gen, 0) would collide with the
            // post-rekey reset value.
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                "frame counter 0 is never valid",
            ));
        }
        Ok(Self {
            version,
            typ,
            flags,
            reserved,
            payload_len,
            generation,
            counter,
        })
    }

    /// Enforce receiver limits. Cheap, no allocation — call it before sizing
    /// anything from `payload_len`.
    pub fn check_limits(&self, limits: &Limits) -> Result<()> {
        if self.payload_len > limits.max_payload {
            return Err(pfatal(
                ErrorCode::PayloadTooLarge,
                format!(
                    "declared payload_len {} exceeds limits.max_payload {}",
                    self.payload_len, limits.max_payload
                ),
            ));
        }
        if self.typ.is_plaintext() && self.payload_len as usize > MAX_HANDSHAKE_MSG {
            return Err(pfatal(
                ErrorCode::PayloadTooLarge,
                format!(
                    "handshake message {} exceeds Noise limit {MAX_HANDSHAKE_MSG}",
                    self.payload_len
                ),
            ));
        }
        if !self.typ.is_plaintext() && self.payload_len > MAX_NOISE_PAYLOAD {
            return Err(pfatal(
                ErrorCode::PayloadTooLarge,
                format!(
                    "payload_len {} exceeds the Noise per-message cap {MAX_NOISE_PAYLOAD}",
                    self.payload_len
                ),
            ));
        }
        Ok(())
    }

    /// Total bytes on the wire for this header.
    pub fn wire_len(&self) -> Result<usize> {
        let body = if self.typ.is_plaintext() {
            self.payload_len as usize
        } else {
            (self.payload_len as usize)
                .checked_add(TAG_LEN)
                .ok_or_else(|| perr(ErrorCode::InvalidArgument, "payload_len overflow"))?
        };
        HEADER_LEN
            .checked_add(body)
            .ok_or_else(|| perr(ErrorCode::InvalidArgument, "frame length overflow"))
    }
}

/// A complete frame: header plus the bytes that followed it on the wire.
///
/// `body` is ciphertext for every type except [`FrameType::Handshake`], where it
/// is the raw Noise message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    header: FrameHeader,
    body: Vec<u8>,
}

impl Frame {
    /// Build a frame from an already-produced body (ciphertext, or a Noise
    /// handshake message). Derives `payload_len` from the body length.
    pub fn from_body(
        typ: FrameType,
        generation: u32,
        counter: u64,
        body: Vec<u8>,
    ) -> Result<Self> {
        let payload_len = if typ.is_plaintext() {
            body.len()
        } else {
            body.len().checked_sub(TAG_LEN).ok_or_else(|| {
                perr(
                    ErrorCode::InvalidArgument,
                    format!("ciphertext body {} < tag {TAG_LEN}", body.len()),
                )
            })?
        };
        let payload_len = u32::try_from(payload_len).map_err(|_| {
            perr(
                ErrorCode::PayloadTooLarge,
                format!("payload_len {payload_len} does not fit in u32"),
            )
        })?;
        if typ.is_plaintext() && counter != 0 {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "handshake frames must use counter 0",
            ));
        }
        if !typ.is_plaintext() && counter == 0 {
            return Err(perr(
                ErrorCode::InvalidArgument,
                "encrypted frames must use counter >= 1",
            ));
        }
        let header = FrameHeader::new(typ, generation, counter, payload_len);
        Self::from_parts(header, body)
    }

    /// Build a frame from explicit parts, validating every invariant the
    /// decoder would validate.
    pub fn from_parts(header: FrameHeader, body: Vec<u8>) -> Result<Self> {
        if header.version != VERSION {
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                format!("frame version {} != {VERSION}", header.version),
            ));
        }
        if header.flags != 0 || header.reserved != 0 {
            return Err(pfatal(
                ErrorCode::ProtocolMismatch,
                "reserved header bytes must be 0",
            ));
        }
        let expected = header.wire_len()? - HEADER_LEN;
        if body.len() != expected {
            return Err(perr(
                ErrorCode::InvalidArgument,
                format!(
                    "declared body length {expected} (payload_len {}) != actual {}",
                    header.payload_len,
                    body.len()
                ),
            ));
        }
        if header.typ.is_plaintext() {
            if header.counter != 0 {
                return Err(perr(
                    ErrorCode::InvalidArgument,
                    "handshake frames must use counter 0",
                ));
            }
            if header.generation != 0 {
                return Err(perr(
                    ErrorCode::InvalidArgument,
                    "handshake frames must use generation 0",
                ));
            }
            if body.len() > MAX_HANDSHAKE_MSG {
                return Err(perr(
                    ErrorCode::PayloadTooLarge,
                    format!("handshake message {} > {MAX_HANDSHAKE_MSG}", body.len()),
                ));
            }
        }
        Ok(Self { header, body })
    }

    /// Header view.
    #[must_use]
    pub fn header(&self) -> &FrameHeader {
        &self.header
    }

    /// Body view: ciphertext, or the Noise message for handshake frames.
    #[must_use]
    pub fn body(&self) -> &[u8] {
        &self.body
    }

    #[must_use]
    pub fn typ(&self) -> FrameType {
        self.header.typ
    }

    #[must_use]
    pub fn generation(&self) -> u32 {
        self.header.generation
    }

    #[must_use]
    pub fn counter(&self) -> u64 {
        self.header.counter
    }

    /// Declared plaintext length (ciphertext is this + 16 for sealed frames).
    #[must_use]
    pub fn payload_len(&self) -> u32 {
        self.header.payload_len
    }

    /// Bytes this frame occupies on the wire.
    #[must_use]
    pub fn encoded_len(&self) -> usize {
        HEADER_LEN + self.body.len()
    }

    /// Serialise to the wire form. Single exact-size allocation.
    #[must_use]
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.encoded_len());
        out.extend_from_slice(&self.header.encode());
        out.extend_from_slice(&self.body);
        out
    }

    /// Decode a complete frame from exactly `bytes.len()` bytes.
    ///
    /// Strict: a truncated frame, a frame with trailing bytes, or a declared
    /// length that disagrees with the buffer is `INVALID_ARGUMENT`. The only
    /// allocation is the exact copy of the body.
    pub fn decode(bytes: &[u8], limits: &Limits) -> Result<Self> {
        let header = FrameHeader::peek(bytes)?;
        header.check_limits(limits)?;
        let expected = header.wire_len()?;
        if bytes.len() != expected {
            return Err(perr(
                ErrorCode::InvalidArgument,
                format!(
                    "declared frame length {expected} != actual {}",
                    bytes.len()
                ),
            ));
        }
        let body = bytes[HEADER_LEN..].to_vec();
        Self::from_parts(header, body)
    }

    /// Header-only probe for streaming readers: read [`HEADER_LEN`] bytes, call
    /// this, then read exactly [`FrameHeader::wire_len`] bytes in total.
    pub fn probe(prefix: &[u8]) -> Result<FrameHeader> {
        FrameHeader::peek(prefix)
    }
}

impl From<ProtocolError> for std::io::Error {
    fn from(e: ProtocolError) -> Self {
        std::io::Error::new(std::io::ErrorKind::InvalidData, e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sealed(typ: FrameType, gen: u32, counter: u64, plaintext_len: usize) -> Frame {
        Frame::from_body(typ, gen, counter, vec![0xAB; plaintext_len + TAG_LEN]).unwrap()
    }

    #[test]
    fn header_is_twenty_bytes_and_layout_is_big_endian() {
        let h = FrameHeader::new(FrameType::Rpc, 0x0102_0304, 0x0102_0304_0506_0708, 0x0000_0400);
        let b = h.encode();
        assert_eq!(HEADER_LEN, b.len());
        assert_eq!(b[0], 1);
        assert_eq!(b[1], 0x02);
        assert_eq!(&b[4..8], &0x0400u32.to_be_bytes());
        assert_eq!(&b[8..12], &0x0102_0304u32.to_be_bytes());
        assert_eq!(&b[12..20], &0x0102_0304_0506_0708u64.to_be_bytes());
        assert_eq!(FrameHeader::peek(&b).unwrap(), h);
    }

    #[test]
    fn roundtrip_preserves_every_field() {
        let f = sealed(FrameType::Event, 7, 42, 33);
        let wire = f.encode();
        let back = Frame::decode(&wire, &Limits::default()).unwrap();
        assert_eq!(back, f);
        assert_eq!(back.payload_len(), 33);
        assert_eq!(back.body().len(), 33 + TAG_LEN);
        assert_eq!(back.encoded_len(), HEADER_LEN + 33 + TAG_LEN);
    }

    #[test]
    fn handshake_frame_has_no_tag() {
        let f = Frame::from_body(FrameType::Handshake, 0, 0, vec![1u8; 96]).unwrap();
        assert_eq!(f.payload_len(), 96);
        assert_eq!(f.body().len(), 96);
        assert_eq!(Frame::decode(&f.encode(), &Limits::default()).unwrap(), f);
    }

    #[test]
    fn declared_vs_actual_length_mismatch_is_rejected() {
        let wire = sealed(FrameType::Rpc, 1, 1, 8).encode();
        // Truncated by one byte.
        assert_eq!(
            Frame::decode(&wire[..wire.len() - 1], &Limits::default())
                .unwrap_err()
                .code(),
            ErrorCode::InvalidArgument
        );
        // Trailing garbage.
        let mut longer = wire.clone();
        longer.push(0);
        assert_eq!(
            Frame::decode(&longer, &Limits::default())
                .unwrap_err()
                .code(),
            ErrorCode::InvalidArgument
        );
    }
}
