//! Vade W0 · C0a spike: the smallest honest UniFFI surface over a real Noise
//! handshake.
//!
//! What this crate proves (or disproves):
//!   1. a synchronous export (`version`);
//!   2. an `async fn` export returning `Result<_, SpikeError>` (`handshake`);
//!   3. a second `async fn` that reuses Rust-side session state
//!      (`send_encrypted`);
//!   4. a callback interface (`EventSink`) that fires from inside those calls.
//!
//! The Noise state machine never crosses the FFI boundary: `Session` owns the
//! `snow::TransportState`, its scratch buffers and the socket; foreign code holds
//! an opaque address string that keys into a process-wide registry.

uniffi::setup_scaffolding!();

pub mod client;
pub mod keys;
pub mod server;
pub mod wire;

#[cfg(test)]
mod testproxy;
#[cfg(test)]
mod tests;

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use serde_json::{json, Value};
use tokio::runtime::Runtime;

use client::{HandshakeReport, Session};

/// Errors that cross the FFI boundary.
///
/// Every variant carries the C-04 `code` plus a human `message`, so Kotlin /
/// Swift / Python can branch on `code` without string-parsing the message and
/// without exposing a Rust `Debug` string.
#[derive(Debug, Clone, thiserror::Error, uniffi::Error)]
pub enum SpikeError {
    #[error("{code}: {message}")]
    Crypto { code: String, message: String },
    #[error("{code}: {message}")]
    Io { code: String, message: String },
    #[error("{code}: {message}")]
    Config { code: String, message: String },
    #[error("{code}: {message}")]
    Protocol { code: String, message: String },
}

impl SpikeError {
    pub fn crypto(code: &str, message: impl Into<String>) -> Self {
        Self::Crypto {
            code: code.into(),
            message: message.into(),
        }
    }
    pub fn io(code: &str, message: impl Into<String>) -> Self {
        Self::Io {
            code: code.into(),
            message: message.into(),
        }
    }
    pub fn config(code: &str, message: impl Into<String>) -> Self {
        Self::Config {
            code: code.into(),
            message: message.into(),
        }
    }
    pub fn protocol(code: &str, message: impl Into<String>) -> Self {
        Self::Protocol {
            code: code.into(),
            message: message.into(),
        }
    }
    pub fn internal(message: impl Into<String>) -> Self {
        Self::Protocol {
            code: "INTERNAL".into(),
            message: message.into(),
        }
    }
    pub fn code(&self) -> &str {
        match self {
            Self::Crypto { code, .. }
            | Self::Io { code, .. }
            | Self::Config { code, .. }
            | Self::Protocol { code, .. } => code,
        }
    }
}

// ---------------------------------------------------------------------------
// Callback interface
// ---------------------------------------------------------------------------

#[uniffi::export(callback_interface)]
pub trait EventSink: Send + Sync {
    fn on_event(&self, json: String);
}

type SinkSlot = Mutex<Option<Arc<dyn EventSink>>>;

fn sink_slot() -> &'static SinkSlot {
    static SLOT: OnceLock<SinkSlot> = OnceLock::new();
    SLOT.get_or_init(|| Mutex::new(None))
}

/// Install the process-wide event sink.
///
/// A global (rather than a per-call parameter) mirrors how the real SDK will
/// work: events also originate from background tasks that no single call owns.
#[uniffi::export]
pub fn set_event_sink(sink: Box<dyn EventSink>) {
    if let Ok(mut slot) = sink_slot().lock() {
        *slot = Some(Arc::from(sink));
    }
}

#[uniffi::export]
pub fn clear_event_sink() {
    if let Ok(mut slot) = sink_slot().lock() {
        *slot = None;
    }
}

fn current_sink() -> Option<Arc<dyn EventSink>> {
    sink_slot().lock().ok().and_then(|s| s.clone())
}

fn emit_with(sink: Option<&dyn EventSink>, kind: &str, fields: Value) {
    let mut obj = serde_json::Map::new();
    obj.insert("event".to_string(), Value::String(kind.to_string()));
    if let Value::Object(extra) = fields {
        for (k, v) in extra {
            obj.insert(k, v);
        }
    }
    let line = Value::Object(obj).to_string();
    if let Some(s) = sink {
        s.on_event(line);
    }
}

pub(crate) fn emit(kind: &str, fields: Value) {
    let sink = current_sink();
    emit_with(sink.as_deref(), kind, fields);
}

// ---------------------------------------------------------------------------
// Session registry: the "handle" the bindings hold is just the address string.
// ---------------------------------------------------------------------------

type Registry = Mutex<HashMap<String, Arc<Session>>>;

fn registry() -> &'static Registry {
    static REG: OnceLock<Registry> = OnceLock::new();
    REG.get_or_init(|| Mutex::new(HashMap::new()))
}

fn insert_session(addr: &str, session: Arc<Session>) {
    if let Ok(mut m) = registry().lock() {
        // Replace a previous connection to the same endpoint instead of leaking it.
        if let Some(old) = m.insert(addr.to_string(), session) {
            old.close();
        }
    }
}

fn lookup_session(addr: &str) -> Result<Arc<Session>, SpikeError> {
    registry()
        .lock()
        .ok()
        .and_then(|m| m.get(addr).cloned())
        .ok_or_else(|| {
            SpikeError::protocol(
                "NOT_FOUND",
                format!("no established session for {addr}; call handshake() first"),
            )
        })
}

/// Number of live sessions (diagnostics / tests).
#[uniffi::export]
pub fn session_count() -> u32 {
    registry().lock().map(|m| m.len() as u32).unwrap_or(0)
}

// ---------------------------------------------------------------------------
// Runtime: exported async fns are polled by the *foreign* event loop, so the
// blocking socket + Noise work runs on our own runtime's blocking pool.
// ---------------------------------------------------------------------------

pub fn rt() -> &'static Runtime {
    static RT: OnceLock<Runtime> = OnceLock::new();
    RT.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("vade-spike-rt")
            .build()
            .expect("failed to build tokio runtime")
    })
}

async fn run_blocking<T, F>(f: F) -> Result<T, SpikeError>
where
    F: FnOnce() -> Result<T, SpikeError> + Send + 'static,
    T: Send + 'static,
{
    let (tx, rx) = tokio::sync::oneshot::channel();
    rt().spawn_blocking(move || {
        let _ = tx.send(f());
    });
    rx.await
        .map_err(|_| SpikeError::internal("blocking worker dropped before replying"))?
}

// ---------------------------------------------------------------------------
// Exported API
// ---------------------------------------------------------------------------

#[uniffi::export]
pub fn version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// Complete a Noise IK handshake with the peer at `addr`, then keep the session.
///
/// Returns a JSON status object (the spike returns a string so the bindings
/// prove string marshalling in both directions).
#[uniffi::export]
pub async fn handshake(addr: String) -> Result<String, SpikeError> {
    let sink = current_sink();
    let addr_for_work = addr.clone();
    let addr_for_event = addr.clone();
    emit_with(
        sink.as_deref(),
        "session.state",
        json!({"state": "connecting", "addr": addr_for_event}),
    );

    let result = run_blocking(move || {
        let keys = keys::load_client_keys()?;
        let prologue = client::default_prologue();
        let mut sk = keys.client_sk;
        let outcome = client::connect(&addr_for_work, &sk, &keys.server_pub, &prologue);
        // The secret has been copied into the HandshakeState at this point.
        keys::wipe(&mut sk);
        let (session, report) = outcome?;
        Ok((Arc::new(session), report))
    })
    .await;

    match result {
        Ok((session, report)) => {
            insert_session(&addr, session);
            let body = report_json(&addr, &report)?;
            emit_with(
                sink.as_deref(),
                "handshake.completed",
                json!({
                    "addr": addr,
                    "handshake_ms": report.handshake_ms,
                    "handshake_hash": keys::hex_encode(&report.handshake_hash),
                }),
            );
            Ok(body)
        }
        Err(e) => {
            emit_with(
                sink.as_deref(),
                "handshake.failed",
                json!({"addr": addr, "code": e.code(), "message": e.to_string()}),
            );
            Err(e)
        }
    }
}

fn report_json(addr: &str, report: &HandshakeReport) -> Result<String, SpikeError> {
    serde_json::to_string(&json!({
        "ok": true,
        "addr": addr,
        "protocol": client::PROTOCOL,
        "handshake_ms": report.handshake_ms,
        "handshake_wire_bytes": report.handshake_wire_bytes,
        "handshake_hash": keys::hex_encode(&report.handshake_hash),
        "server_static_pub": keys::hex_encode(&report.remote_static),
    }))
    .map_err(|e| SpikeError::internal(format!("json: {e}")))
}

/// Send one plaintext through the established Noise transport channel and
/// return the peer's echo.
#[uniffi::export]
pub async fn send_encrypted(addr: String, plaintext: Vec<u8>) -> Result<Vec<u8>, SpikeError> {
    let sink = current_sink();
    let session = lookup_session(&addr)?;
    let sent_len = plaintext.len();
    let addr_for_work = addr.clone();
    let reply = run_blocking(move || session.round_trip(&plaintext)).await?;
    emit_with(
        sink.as_deref(),
        "message.sent",
        json!({"addr": addr_for_work, "bytes": sent_len, "ciphertext_bytes": sent_len + 16}),
    );
    emit_with(
        sink.as_deref(),
        "message.received",
        json!({"addr": addr, "bytes": reply.len()}),
    );
    Ok(reply)
}

#[uniffi::export]
pub fn close_session(addr: String) -> bool {
    if let Ok(mut m) = registry().lock() {
        if let Some(s) = m.remove(&addr) {
            s.close();
            emit("session.closed", json!({"addr": addr}));
            return true;
        }
    }
    false
}
