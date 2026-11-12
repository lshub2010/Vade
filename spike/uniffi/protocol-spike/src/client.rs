//! Blocking Noise IK client.
//!
//! Design point under test (T2): `snow::HandshakeState` / `TransportState` take
//! caller-owned `&mut [u8]` buffers, so they cannot be handed across the FFI as
//! borrowed values. Here the state machine plus its buffers and the socket live
//! inside `Session`; the FFI layer only ever passes an address string that keys
//! into a registry ("handle"), and takes an `Arc<Session>` clone for the
//! duration of a call.

use std::io;
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use snow::params::NoiseParams;
use snow::{Builder, TransportState};

use crate::wire::{self, MAX_NOISE_MSG};
use crate::SpikeError;

/// Fixed parameter set for the spike (design §2.2).
pub const PROTOCOL: &str = "Noise_IK_25519_ChaChaPoly_BLAKE2s";

pub const PROLOGUE_PREFIX: &[u8] = b"vade/v1";

/// Role byte for a connection initiated by the core side.
pub const ROLE_CORE_INITIATES: u8 = 1;
/// Role byte for a connection initiated by the module side.
pub const ROLE_MODULE_INITIATES: u8 = 2;

pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
pub const IO_TIMEOUT: Duration = Duration::from_secs(5);

/// design §2.2: `prologue = b"vade/v1" || role_byte`.
///
/// Both peers MUST feed the *same* bytes: Noise hashes the prologue into the
/// handshake, so a mismatch fails the handshake by construction (proven in
/// `tests::prologue_mismatch_is_rejected`). The role byte therefore describes
/// "who initiated this connection", which both sides know out of band — it is
/// not "my own role".
pub fn prologue(connection_role: u8) -> Vec<u8> {
    let mut p = PROLOGUE_PREFIX.to_vec();
    p.push(connection_role);
    p
}

/// Prologue used by the spike's demos: core initiates.
pub fn default_prologue() -> Vec<u8> {
    prologue(ROLE_CORE_INITIATES)
}

#[derive(Debug)]
pub struct HandshakeReport {
    pub handshake_ms: f64,
    pub handshake_hash: Vec<u8>,
    pub remote_static: Vec<u8>,
    pub handshake_wire_bytes: usize,
}

pub struct Session {
    inner: Mutex<Inner>,
}

impl std::fmt::Debug for Session {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Never print key material or the Noise state.
        f.debug_struct("Session")
            .field("addr", &self.addr())
            .finish_non_exhaustive()
    }
}

struct Inner {
    transport: TransportState,
    stream: TcpStream,
    /// Owned scratch buffer: this is what replaces the borrowed-slice problem.
    buf: Vec<u8>,
    addr: String,
}

impl Session {
    /// Send one plaintext through the Noise transport channel, read one reply frame.
    pub fn round_trip(&self, plaintext: &[u8]) -> Result<Vec<u8>, SpikeError> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| SpikeError::internal("session mutex poisoned"))?;
        let Inner {
            transport,
            stream,
            buf,
            ..
        } = &mut *inner;

        let n = transport
            .write_message(plaintext, buf)
            .map_err(map_snow)?
            ;
        wire::write_frame(stream, &buf[..n]).map_err(map_io)?;

        let frame = wire::read_frame(stream).map_err(map_io)?;
        let k = transport.read_message(&frame, buf).map_err(map_snow)?;
        Ok(buf[..k].to_vec())
    }

    pub fn addr(&self) -> String {
        self.inner
            .lock()
            .map(|i| i.addr.clone())
            .unwrap_or_else(|_| "<poisoned>".into())
    }

    pub fn close(&self) {
        if let Ok(inner) = self.inner.lock() {
            let _ = inner.stream.shutdown(std::net::Shutdown::Both);
        }
    }
}

pub fn params() -> Result<NoiseParams, SpikeError> {
    PROTOCOL
        .parse::<NoiseParams>()
        .map_err(|e| SpikeError::protocol("PROTOCOL_MISMATCH", format!("bad noise params: {e:?}")))
}

/// Complete the IK handshake as initiator (1-RTT: msg1 -> msg2).
///
/// `client_sk` is the initiator static secret, `server_pub` the pinned responder
/// static public key (the whitelist entry from design §2.2). The key material is
/// copied into the `HandshakeState` by `build_initiator()`.
pub fn connect(
    addr: &str,
    client_sk: &[u8],
    server_pub: &[u8],
    prologue: &[u8],
) -> Result<(Session, HandshakeReport), SpikeError> {
    if client_sk.len() != 32 || server_pub.len() != 32 {
        return Err(SpikeError::config(
            "INVALID_ARGUMENT",
            "static keys must be 32 bytes (X25519)",
        ));
    }

    let params = params()?;
    let mut hs = Builder::new(params)
        .local_private_key(client_sk)
        .and_then(|b| b.remote_public_key(server_pub))
        .and_then(|b| b.prologue(prologue))
        .map_err(map_snow)?
        .build_initiator()
        .map_err(map_snow)?;

    let sockaddr = addr
        .to_socket_addrs()
        .map_err(|e| SpikeError::config("INVALID_ARGUMENT", format!("bad addr {addr:?}: {e}")))?
        .next()
        .ok_or_else(|| SpikeError::config("INVALID_ARGUMENT", format!("addr {addr:?} resolved to nothing")))?;

    let mut stream = TcpStream::connect_timeout(&sockaddr, CONNECT_TIMEOUT).map_err(map_io)?;
    let _ = stream.set_nodelay(true);
    stream
        .set_read_timeout(Some(IO_TIMEOUT))
        .and_then(|_| stream.set_write_timeout(Some(IO_TIMEOUT)))
        .map_err(map_io)?;

    let mut send_buf = vec![0u8; MAX_NOISE_MSG];
    let mut recv_buf = vec![0u8; MAX_NOISE_MSG];
    let mut wire_bytes = 0usize;
    let started = Instant::now();

    // -> e
    let n = hs.write_message(&[], &mut send_buf).map_err(map_snow)?;
    wire::write_frame(&mut stream, &send_buf[..n]).map_err(map_io)?;
    wire_bytes += n + 4;

    // <- e, ee, se  (carries the responder static key)
    let frame = wire::read_frame(&mut stream).map_err(map_io)?;
    wire_bytes += frame.len() + 4;
    let payload_len = hs.read_message(&frame, &mut recv_buf).map_err(map_snow)?;
    if payload_len != 0 {
        return Err(SpikeError::protocol(
            "PROTOCOL_MISMATCH",
            "unexpected payload in handshake message 2",
        ));
    }

    // Pin check: the static key we got must be the one we whitelisted. snow has
    // already authenticated it via the IK pattern; this catches config mistakes.
    let remote = hs.get_remote_static().ok_or_else(|| {
        SpikeError::crypto("AUTH_FAILED", "handshake completed without a remote static key")
    })?;
    if remote != server_pub {
        return Err(SpikeError::crypto(
            "AUTH_FAILED",
            "server static key does not match the pinned key",
        ));
    }

    // IK is 1-RTT: msg1 carried e, es, s, ss; msg2 carried e, ee, se.
    // The handshake is complete on the initiator side after msg2.
    if !hs.is_handshake_finished() {
        return Err(SpikeError::protocol(
            "PROTOCOL_MISMATCH",
            "IK handshake not finished after two messages",
        ));
    }

    let handshake_hash = hs.get_handshake_hash().to_vec();
    let remote_static = remote.to_vec();
    let transport = hs.into_transport_mode().map_err(map_snow)?;
    let handshake_ms = started.elapsed().as_secs_f64() * 1000.0;

    Ok((
        Session {
            inner: Mutex::new(Inner {
                transport,
                stream,
                buf: vec![0u8; MAX_NOISE_MSG],
                addr: addr.to_string(),
            }),
        },
        HandshakeReport {
            handshake_ms,
            handshake_hash,
            remote_static,
            handshake_wire_bytes: wire_bytes,
        },
    ))
}

pub fn map_snow(e: snow::Error) -> SpikeError {
    use snow::Error as E;
    let code = match e {
        E::Decrypt => "AUTH_FAILED",
        E::Dh => "AUTH_FAILED",
        E::Pattern(_) => "PROTOCOL_MISMATCH",
        E::Init(_) => "PROTOCOL_MISMATCH",
        E::Prereq(_) => "PROTOCOL_MISMATCH",
        E::State(_) => "PROTOCOL_MISMATCH",
        E::Input => "INVALID_ARGUMENT",
        E::Rng => "INTERNAL",
        _ => "INTERNAL",
    };
    let message = match e {
        E::Decrypt => "noise: ciphertext failed authentication (tampered or wrong key)".to_string(),
        other => format!("noise: {other:?}"),
    };
    SpikeError::crypto(code, message)
}

pub fn map_io(e: io::Error) -> SpikeError {
    let code = match e.kind() {
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => "TIMEOUT",
        io::ErrorKind::ConnectionRefused
        | io::ErrorKind::ConnectionReset
        | io::ErrorKind::ConnectionAborted
        | io::ErrorKind::UnexpectedEof
        | io::ErrorKind::BrokenPipe
        | io::ErrorKind::NotConnected => "UNAVAILABLE",
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData => "PROTOCOL_MISMATCH",
        _ => "INTERNAL",
    };
    SpikeError::io(code, format!("io: {e}"))
}
