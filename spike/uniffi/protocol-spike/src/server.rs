//! Tokio test server for the spike (T2).
//!
//! One IK responder per TCP connection; after the handshake it decrypts each
//! frame, re-encrypts the same plaintext and sends it back (echo). A frame that
//! fails authentication closes the connection without a reply and bumps
//! `auth_failures` — that counter is what the tamper tests assert on.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use snow::{Builder, TransportState};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::client::{self, map_snow};
use crate::wire::{self, MAX_NOISE_MSG};
use crate::SpikeError;
use crate::{client::prologue as make_prologue, rt};

#[derive(Default)]
pub struct ServerStats {
    pub accepted: AtomicU64,
    pub handshakes: AtomicU64,
    pub auth_failures: AtomicU64,
    pub echoes: AtomicU64,
}

pub struct ServerHandle {
    pub addr: String,
    pub static_public: Vec<u8>,
    pub stats: Arc<ServerStats>,
    shutdown: Option<oneshot::Sender<()>>,
    join: Option<JoinHandle<()>>,
}

impl ServerHandle {
    pub fn auth_failures(&self) -> u64 {
        self.stats.auth_failures.load(Ordering::Relaxed)
    }
    pub fn echoes(&self) -> u64 {
        self.stats.echoes.load(Ordering::Relaxed)
    }
    pub fn handshakes(&self) -> u64 {
        self.stats.handshakes.load(Ordering::Relaxed)
    }
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        if let Some(tx) = self.shutdown.take() {
            let _ = tx.send(());
        }
        if let Some(join) = self.join.take() {
            join.abort();
        }
    }
}

/// Start the test server on an ephemeral loopback port.
///
/// Must be called from a thread that is *not* inside a tokio runtime (it uses
/// the crate-wide runtime and `block_on` for setup).
pub fn start_server(
    server_sk: &[u8],
    client_pub: &[u8],
    prologue: Vec<u8>,
) -> Result<ServerHandle, SpikeError> {
    if server_sk.len() != 32 || client_pub.len() != 32 {
        return Err(SpikeError::config(
            "INVALID_ARGUMENT",
            "static keys must be 32 bytes (X25519)",
        ));
    }
    let server_public = {
        // Derive the public half from the secret so callers cannot pass a
        // mismatched pair by accident.
        public_from_secret(server_sk)?
    };
    let sk = server_sk.to_vec();
    let client_pub = client_pub.to_vec();

    let (bound_tx, bound_rx) = std::sync::mpsc::channel();
    rt().spawn(async move {
        let listener = match TcpListener::bind("127.0.0.1:0").await {
            Ok(l) => l,
            Err(e) => {
                let _ = bound_tx.send(Err(SpikeError::io(
                    "UNAVAILABLE",
                    format!("bind failed: {e}"),
                )));
                return;
            }
        };
        let addr = match listener.local_addr() {
            Ok(a) => a.to_string(),
            Err(e) => {
                let _ = bound_tx.send(Err(SpikeError::io("INTERNAL", format!("local_addr: {e}"))));
                return;
            }
        };
        let stats = Arc::new(ServerStats::default());
        let (shut_tx, shut_rx) = oneshot::channel();
        let join = tokio::spawn(accept_loop(
            listener,
            Arc::new(sk),
            Arc::new(client_pub),
            Arc::new(prologue),
            stats.clone(),
            shut_rx,
        ));
        let _ = bound_tx.send(Ok((addr, stats, shut_tx, join)));
    });

    let (addr, stats, shutdown, join) = bound_rx
        .recv()
        .map_err(|_| SpikeError::internal("server task dropped before binding"))??;
    Ok(ServerHandle {
        addr,
        static_public: server_public,
        stats,
        shutdown: Some(shutdown),
        join: Some(join),
    })
}

pub fn public_from_secret(sk: &[u8]) -> Result<Vec<u8>, SpikeError> {
    // Derive via snow's own DH implementation so the spike never adds a second
    // crypto crate just to compute the public half of a static key.
    use snow::resolvers::{CryptoResolver, DefaultResolver};
    let params = client::params()?;
    let resolver = DefaultResolver;
    let mut dh = resolver
        .resolve_dh(&params.dh)
        .ok_or_else(|| SpikeError::internal("snow resolver has no 25519 DH"))?;
    dh.set(sk);
    Ok(dh.pubkey().to_vec())
}

async fn accept_loop(
    listener: TcpListener,
    server_sk: Arc<Vec<u8>>,
    client_pub: Arc<Vec<u8>>,
    prologue: Arc<Vec<u8>>,
    stats: Arc<ServerStats>,
    mut shutdown: oneshot::Receiver<()>,
) {
    loop {
        tokio::select! {
            _ = &mut shutdown => break,
            accepted = listener.accept() => {
                match accepted {
                    Ok((stream, _peer)) => {
                        stats.accepted.fetch_add(1, Ordering::Relaxed);
                        let sk = server_sk.clone();
                        let cp = client_pub.clone();
                        let pl = prologue.clone();
                        let st = stats.clone();
                        tokio::spawn(async move {
                            let _ = handle_conn(stream, &sk, &cp, &pl, &st).await;
                        });
                    }
                    Err(_) => break,
                }
            }
        }
    }
}

async fn handle_conn(
    mut stream: TcpStream,
    server_sk: &[u8],
    client_pub: &[u8],
    prologue: &[u8],
    stats: &ServerStats,
) -> Result<(), SpikeError> {
    let _ = stream.set_nodelay(true);
    let params = client::params()?;
    let mut hs = Builder::new(params)
        .local_private_key(server_sk)
        .and_then(|b| b.remote_public_key(client_pub))
        .and_then(|b| b.prologue(prologue))
        .map_err(map_snow)?
        .build_responder()
        .map_err(map_snow)?;

    let mut buf = vec![0u8; MAX_NOISE_MSG];

    // <- e, es, s, ss
    let msg1 = wire::read_frame_async(&mut stream).await.map_err(client::map_io)?;
    if let Err(e) = hs.read_message(&msg1, &mut buf) {
        stats.auth_failures.fetch_add(1, Ordering::Relaxed);
        return Err(map_snow(e));
    }

    // IK's responder LEARNS the initiator's static key from msg1 (it is not
    // authenticated against the pre-shared value by snow, and any keypair can
    // complete `es`). The whitelist check is therefore ours to make, and it
    // must happen before we treat the peer as authenticated.
    match hs.get_remote_static() {
        Some(remote) if remote == client_pub => {}
        _ => {
            stats.auth_failures.fetch_add(1, Ordering::Relaxed);
            return Err(SpikeError::crypto(
                "AUTH_FAILED",
                "initiator static key is not in the whitelist",
            ));
        }
    }

    // -> e, ee, se
    let n = hs.write_message(&[], &mut buf).map_err(map_snow)?;
    wire::write_frame_async(&mut stream, &buf[..n])
        .await
        .map_err(client::map_io)?;

    // IK is 1-RTT: msg1 carried e, es, s, ss (including the initiator's static
    // key, encrypted), msg2 carried e, ee, se. Nothing else to read here.
    if !hs.is_handshake_finished() {
        stats.auth_failures.fetch_add(1, Ordering::Relaxed);
        return Err(SpikeError::protocol(
            "PROTOCOL_MISMATCH",
            "IK responder not finished after msg2",
        ));
    }

    let mut transport: TransportState = hs.into_transport_mode().map_err(map_snow)?;
    stats.handshakes.fetch_add(1, Ordering::Relaxed);

    loop {
        let frame = match wire::read_frame_async(&mut stream).await {
            Ok(f) => f,
            // Clean EOF or a dropped client: not an auth failure.
            Err(_) => return Ok(()),
        };
        let k = match transport.read_message(&frame, &mut buf) {
            Ok(k) => k,
            Err(e) => {
                // Tampered ciphertext / bad tag: never reply, just drop the peer.
                stats.auth_failures.fetch_add(1, Ordering::Relaxed);
                return Err(map_snow(e));
            }
        };
        let reply = buf[..k].to_vec();
        let n = transport.write_message(&reply, &mut buf).map_err(map_snow)?;
        wire::write_frame_async(&mut stream, &buf[..n])
            .await
            .map_err(client::map_io)?;
        stats.echoes.fetch_add(1, Ordering::Relaxed);
    }
}

/// Prologue the responder must use. It is identical to the initiator's: the
/// role byte describes the connection, not the local side (design §2.2).
pub fn responder_prologue() -> Vec<u8> {
    make_prologue(crate::client::ROLE_CORE_INITIATES)
}
