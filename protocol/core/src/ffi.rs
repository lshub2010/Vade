//! UniFFI surface (proven feasible by C0a).
//!
//! Rules taken from C0a §六:
//! * no lifetimes, generics or trait objects cross the boundary;
//! * every object is Rust-owned behind an `Arc`, the binding only holds a handle;
//! * errors are one flat enum carrying the C-04 `code` as a string, because
//!   uniffi 0.32 only derives `Error` for enums, not for structs.
//!
//! `Frame` is deliberately **not** exported: bindings deal in `Vec<u8>` at the
//! seal/open boundary and never need to inspect a header. `FfiTransport` is the
//! only hot path.

use std::sync::{Arc, Mutex, MutexGuard};

use crate::error::ProtocolError;
use crate::frame::{Frame, FrameType};
use crate::handshake::{ConnectionRole, Handshake, Mode, Role};
use crate::identity::Identity;
use crate::transport::Transport;

/// The only error that crosses FFI. Mirrors C-04: `code`, `message`,
/// `retriable`, plus the protocol-core specific `fatal`.
#[derive(Debug, uniffi::Error)]
pub enum FfiError {
    /// A `ProtocolError` from the core.
    Protocol {
        /// C-04 code, e.g. `"PERMISSION_DENIED"`.
        code: String,
        /// Human readable, ≤ 300 chars, never contains key material.
        message: String,
        /// `true` → the caller must drop the connection.
        fatal: bool,
        /// C-04 retriable flag for this code.
        retriable: bool,
    },
}

impl From<ProtocolError> for FfiError {
    fn from(e: ProtocolError) -> Self {
        FfiError::Protocol {
            code: e.code_str().to_string(),
            message: e.message().to_string(),
            fatal: e.is_fatal(),
            retriable: e.retriable(),
        }
    }
}

impl std::fmt::Display for FfiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FfiError::Protocol {
                code,
                message,
                fatal,
                ..
            } => write!(
                f,
                "{code}: {message}{}",
                if *fatal { " (fatal)" } else { "" }
            ),
        }
    }
}

impl std::error::Error for FfiError {}

fn poisoned() -> FfiError {
    FfiError::Protocol {
        code: "INTERNAL".to_string(),
        message: "lock poisoned".to_string(),
        fatal: false,
        retriable: false,
    }
}

/// Crate version.
#[uniffi::export]
pub fn version() -> String {
    crate::version()
}

/// Major protocol version (always 1 in v1).
#[uniffi::export]
pub fn protocol_version() -> u32 {
    crate::protocol_version()
}

/// A long-term X25519 identity.
#[derive(uniffi::Object)]
pub struct FfiIdentity {
    inner: Identity,
}

#[uniffi::export]
impl FfiIdentity {
    /// Generate from the OS CSPRNG.
    #[uniffi::constructor]
    pub fn generate() -> Result<Arc<Self>, FfiError> {
        Ok(Arc::new(Self {
            inner: Identity::generate().map_err(FfiError::from)?,
        }))
    }

    /// Import the 32-byte secret from hex (the 0600 seed file).
    #[uniffi::constructor]
    pub fn from_secret_hex(secret_hex: String) -> Result<Arc<Self>, FfiError> {
        Ok(Arc::new(Self {
            inner: Identity::from_secret_hex(&secret_hex).map_err(FfiError::from)?,
        }))
    }

    pub fn public_key_hex(&self) -> String {
        self.inner.public_key_hex()
    }

    /// `BLAKE2s-256(public_key)`, hex — the whitelist / `core_pub_fp` value.
    pub fn fingerprint_hex(&self) -> String {
        self.inner.fingerprint_hex()
    }

    /// Seed-file export. Never log this.
    pub fn to_secret_hex(&self) -> String {
        self.inner.to_secret_hex()
    }
}

impl std::fmt::Debug for FfiIdentity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FfiIdentity").finish_non_exhaustive()
    }
}

/// A handshake in progress. Consumed by [`FfiHandshake::into_transport`].
#[derive(uniffi::Object)]
pub struct FfiHandshake {
    inner: Mutex<Option<Handshake>>,
}

#[uniffi::export]
impl FfiHandshake {
    /// Start a handshake.
    ///
    /// * `mode`: `"ik"` or `"xxpsk3"`.
    /// * `role`: `"initiator"` or `"responder"`.
    /// * `connection`: `"core_initiated"` or `"module_initiated"` — **both
    ///   peers must pass the same value**, it is the prologue role byte.
    #[uniffi::constructor]
    pub fn new(
        mode: String,
        role: String,
        connection: String,
        local: Arc<FfiIdentity>,
        remote_pub: Option<Vec<u8>>,
        psk: Option<Vec<u8>>,
    ) -> Result<Arc<Self>, FfiError> {
        let mode = match mode.as_str() {
            "ik" => Mode::Ik,
            "xxpsk3" => Mode::XxPsk,
            other => {
                return Err(bad_arg(&format!("unknown handshake mode {other:?}")))
            }
        };
        let role = match role.as_str() {
            "initiator" => Role::Initiator,
            "responder" => Role::Responder,
            other => return Err(bad_arg(&format!("unknown role {other:?}"))),
        };
        let connection = match connection.as_str() {
            "core_initiated" => ConnectionRole::CoreInitiated,
            "module_initiated" => ConnectionRole::ModuleInitiated,
            other => return Err(bad_arg(&format!("unknown connection role {other:?}"))),
        };
        let inner = Handshake::new(
            mode,
            role,
            connection,
            &local.inner,
            remote_pub.as_deref(),
            psk.as_deref(),
        )
        .map_err(FfiError::from)?;
        Ok(Arc::new(Self {
            inner: Mutex::new(Some(inner)),
        }))
    }

    pub fn write(&self, payload: Vec<u8>) -> Result<Vec<u8>, FfiError> {
        self.with(|hs| hs.write(&payload)).map_err(FfiError::from)
    }

    pub fn read(&self, message: Vec<u8>) -> Result<Vec<u8>, FfiError> {
        self.with(|hs| hs.read(&message)).map_err(FfiError::from)
    }

    pub fn is_finished(&self) -> Result<bool, FfiError> {
        self.with(|hs| Ok(hs.is_finished())).map_err(FfiError::from)
    }

    pub fn remote_static(&self) -> Result<Option<Vec<u8>>, FfiError> {
        self.with(|hs| Ok(hs.remote_static())).map_err(FfiError::from)
    }

    pub fn handshake_hash(&self) -> Result<Vec<u8>, FfiError> {
        self.with(|hs| Ok(hs.handshake_hash())).map_err(FfiError::from)
    }

    /// Consume the handshake and enter transport mode.
    pub fn into_transport(self: Arc<Self>) -> Result<Arc<FfiTransport>, FfiError> {
        let mut guard = self.inner.lock().map_err(|_| poisoned())?;
        let hs = guard.take().ok_or_else(|| bad_arg("handshake already consumed"))?;
        drop(guard);
        Ok(Arc::new(FfiTransport {
            inner: Mutex::new(hs.into_transport().map_err(FfiError::from)?),
        }))
    }
}

impl FfiHandshake {
    fn with<T>(&self, f: impl FnOnce(&mut Handshake) -> crate::error::Result<T>) -> crate::error::Result<T> {
        let mut guard = self
            .inner
            .lock()
            .map_err(|_| ProtocolError::new(crate::error::ErrorCode::Internal, "poisoned lock"))?;
        let hs = guard.as_mut().ok_or_else(|| {
            ProtocolError::new(
                crate::error::ErrorCode::Internal,
                "handshake already consumed",
            )
        })?;
        f(hs)
    }
}

impl std::fmt::Debug for FfiHandshake {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FfiHandshake").finish_non_exhaustive()
    }
}

/// An established session.
#[derive(uniffi::Object)]
pub struct FfiTransport {
    inner: Mutex<Transport>,
}

#[uniffi::export]
impl FfiTransport {
    /// Seal a plaintext into one complete wire frame. `frame_type` is the frame
    /// type byte (2 = RPC, 3 = event, 4 = heartbeat, 5 = close).
    pub fn seal_frame(&self, frame_type: u8, plaintext: Vec<u8>) -> Result<Vec<u8>, FfiError> {
        let typ = FrameType::from_u8(frame_type).map_err(FfiError::from)?;
        let mut t = self.lock()?;
        Ok(t.seal_frame(typ, &plaintext)
            .map_err(FfiError::from)?
            .encode())
    }

    /// Open one complete wire frame and return its plaintext.
    pub fn open_frame(&self, wire: Vec<u8>) -> Result<Vec<u8>, FfiError> {
        let mut t = self.lock()?;
        let limits = t.limits();
        let frame = Frame::decode(&wire, &limits).map_err(FfiError::from)?;
        t.open_frame(&frame).map_err(FfiError::from)
    }

    pub fn send_generation(&self) -> Result<u32, FfiError> {
        Ok(self.lock()?.send_generation())
    }

    pub fn recv_generation(&self) -> Result<u32, FfiError> {
        Ok(self.lock()?.recv_generation())
    }

    pub fn handshake_hash(&self) -> Result<Vec<u8>, FfiError> {
        Ok(self.lock()?.handshake_hash().to_vec())
    }

    /// Build a `0x06 rekey` frame and advance the send generation.
    pub fn begin_rekey(&self) -> Result<Vec<u8>, FfiError> {
        let mut t = self.lock()?;
        Ok(t.begin_rekey().map_err(FfiError::from)?.encode())
    }
}

impl FfiTransport {
    fn lock(&self) -> Result<MutexGuard<'_, Transport>, FfiError> {
        self.inner.lock().map_err(|_| poisoned())
    }
}

impl std::fmt::Debug for FfiTransport {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FfiTransport").finish_non_exhaustive()
    }
}

fn bad_arg(msg: &str) -> FfiError {
    FfiError::Protocol {
        code: "INVALID_ARGUMENT".to_string(),
        message: msg.to_string(),
        fatal: false,
        retriable: false,
    }
}
