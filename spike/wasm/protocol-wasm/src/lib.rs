//! C0b spike: the Vade protocol core reduced to its Wasm-relevant boundary.
//!
//! Design rule under test (design.md §8 + §9.4, task C0b T2):
//!   the Rust core owns *framing + cryptography only*; the transport (WebSocket)
//!   lives in the host. Rust never opens a socket, it asks the host to send bytes
//!   and awaits bytes back.
//!
//! Exports, intentionally shaped like the future SDK surface:
//!   - `handshake(transport, psk_hex) -> Promise<string>`   async, awaits a JS Promise
//!   - `encrypt(plaintext) -> Uint8Array`
//!   - `on_event(cb)`                                        host callback
//!   - `Peer`                                                explicit two-sided state machine
//!                                                           (used by the Node test as the
//!                                                           "core" side of the handshake)

use std::cell::RefCell;

use js_sys::{Function, Promise, Reflect, Uint8Array};
use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::JsFuture;

const PATTERN_IK: &str = "Noise_IK_25519_ChaChaPoly_BLAKE2s";
const PATTERN_IK_PSK2: &str = "Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s";
const MAX_FRAME: usize = 64 * 1024;

thread_local! {
    static SESSION: RefCell<Option<snow::TransportState>> = const { RefCell::new(None) };
    static EVENT_CB: RefCell<Option<Function>> = const { RefCell::new(None) };
    static LAST_ERROR: RefCell<Option<String>> = const { RefCell::new(None) };
}

#[wasm_bindgen(start)]
pub fn start() {
    console_error_panic_hook::set_once();
}

fn err(e: impl std::fmt::Display) -> JsValue {
    JsValue::from_str(&e.to_string())
}

fn set_error(msg: &str) {
    LAST_ERROR.with(|c| *c.borrow_mut() = Some(msg.to_owned()));
}

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

fn from_hex(s: &str) -> Result<Vec<u8>, String> {
    let s = s.trim();
    if s.len() % 2 != 0 {
        return Err("hex string has odd length".into());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    for i in (0..bytes.len()).step_by(2) {
        let hi = (bytes[i] as char).to_digit(16).ok_or("bad hex")?;
        let lo = (bytes[i + 1] as char).to_digit(16).ok_or("bad hex")?;
        out.push((hi * 16 + lo) as u8);
    }
    Ok(out)
}

fn build_params(psk: Option<&[u8]>) -> Result<snow::params::NoiseParams, String> {
    let s = if psk.is_some() { PATTERN_IK_PSK2 } else { PATTERN_IK };
    s.parse::<snow::params::NoiseParams>()
        .map_err(|e| format!("bad noise params: {e}"))
}

/// Which side of the handshake a `Peer` is.
#[wasm_bindgen]
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Initiator = 0,
    Responder = 1,
}

/// A protocol peer: handshake state machine -> transport state.
///
/// This is the shape C1 should adopt for the core: no IO, caller pushes bytes in
/// and pulls bytes out.
#[wasm_bindgen]
pub struct Peer {
    hs: Option<snow::HandshakeState>,
    ts: Option<snow::TransportState>,
    local_static_pub: Vec<u8>,
    role: Role,
}

#[wasm_bindgen]
impl Peer {
    /// Initiator. `remote_static` is the 32-byte X25519 static public key of the
    /// responder, delivered out-of-band (in Vade: inside the pair code).
    #[wasm_bindgen(constructor)]
    pub fn new(role: Role, remote_static: Option<Vec<u8>>, psk_hex: Option<String>) -> Result<Peer, JsValue> {
        let psk = match psk_hex.as_deref() {
            Some(h) => Some(from_hex(h).map_err(err)?),
            None => None,
        };
        if let Some(p) = psk.as_deref() {
            if p.len() != 32 {
                return Err(err("psk must be 32 bytes"));
            }
        }
        let params = build_params(psk.as_deref()).map_err(err)?;

        // Hoisted above `builder`: `Builder::remote_public_key` borrows for the
        // builder's whole lifetime, so the bytes must outlive it.
        let remote = match role {
            Role::Initiator => {
                let r = remote_static.ok_or_else(|| err("initiator requires remote static key"))?;
                if r.len() != 32 {
                    return Err(err("remote static key must be 32 bytes"));
                }
                Some(r)
            }
            Role::Responder => None,
        };

        let mut builder = snow::Builder::new(params);
        if let Some(p) = psk.as_deref() {
            builder = builder.psk(2, p);
        }

        let keypair = builder.generate_keypair().map_err(|e| {
            set_error(&format!("generate_keypair failed: {e}"));
            err(format!("generate_keypair failed: {e}"))
        })?;
        builder = builder.local_private_key(&keypair.private);
        if let Some(r) = remote.as_deref() {
            builder = builder.remote_public_key(r);
        }

        let hs = match role {
            Role::Initiator => builder.build_initiator().map_err(err)?,
            Role::Responder => builder.build_responder().map_err(err)?,
        };

        Ok(Peer {
            hs: Some(hs),
            ts: None,
            local_static_pub: keypair.public,
            role,
        })
    }

    /// The peer's own static public key (hex). Test/DI use only.
    pub fn static_public_key_hex(&self) -> String {
        hex(&self.local_static_pub)
    }

    pub fn role(&self) -> Role {
        self.role
    }

    pub fn is_established(&self) -> bool {
        self.ts.is_some()
    }

    /// Produce the next handshake message.
    ///
    /// The *responder* finishes the handshake by writing msg2 (not by reading),
    /// so the promotion to transport mode has to happen here as well — otherwise
    /// `is_established()` stays false forever on the responder side and every
    /// `decrypt()` fails with "not established".
    pub fn write_handshake(&mut self) -> Result<Vec<u8>, JsValue> {
        let hs = self.hs.as_mut().ok_or_else(|| err("handshake already finished"))?;
        let mut buf = vec![0u8; MAX_FRAME];
        let n = match hs.write_message(&[], &mut buf) {
            Ok(n) => n,
            Err(e) => {
                set_error(&format!("handshake write failed: {e}"));
                return Err(err(format!("handshake write failed: {e}")));
            }
        };
        buf.truncate(n);
        if hs.is_handshake_finished() {
            let hs = self.hs.take().expect("checked above");
            self.ts = Some(hs.into_transport_mode().map_err(err)?);
        }
        Ok(buf)
    }

    /// Consume a handshake message, returning its (usually empty) payload.
    /// Call `write_handshake()` afterwards if `is_established()` is still false.
    pub fn read_handshake(&mut self, msg: &[u8]) -> Result<Vec<u8>, JsValue> {
        let hs = self.hs.as_mut().ok_or_else(|| err("handshake already finished"))?;
        let mut buf = vec![0u8; MAX_FRAME];
        let n = match hs.read_message(msg, &mut buf) {
            Ok(n) => n,
            Err(e) => {
                set_error(&format!("handshake read failed: {e}"));
                return Err(err(format!("handshake read failed: {e}")));
            }
        };
        buf.truncate(n);
        if hs.is_handshake_finished() {
            let hs = self.hs.take().expect("checked above");
            self.ts = Some(hs.into_transport_mode().map_err(err)?);
        }
        Ok(buf)
    }

    /// AEAD seal with the session key; returns ciphertext (header-less for the
    /// spike — C1 puts the framing header in front).
    pub fn encrypt(&mut self, plaintext: &[u8]) -> Result<Vec<u8>, JsValue> {
        let ts = self.ts.as_mut().ok_or_else(|| err("not established"))?;
        let mut buf = vec![0u8; plaintext.len() + 16];
        let n = ts.write_message(plaintext, &mut buf).map_err(err)?;
        buf.truncate(n);
        Ok(buf)
    }

    pub fn decrypt(&mut self, ciphertext: &[u8]) -> Result<Vec<u8>, JsValue> {
        let ts = self.ts.as_mut().ok_or_else(|| err("not established"))?;
        let mut buf = vec![0u8; ciphertext.len()];
        let n = match ts.read_message(ciphertext, &mut buf) {
            Ok(n) => n,
            Err(e) => {
                set_error(&format!("decrypt failed: {e}"));
                return Err(err(format!("decrypt failed: {e}")));
            }
        };
        buf.truncate(n);
        Ok(buf)
    }
}

/// Register the host callback for core -> UI events. Events are the JSON
/// objects `{name, payload}`; the spike emits `handshake.done` and `frame.received`.
#[wasm_bindgen]
pub fn on_event(cb: Function) {
    EVENT_CB.with(|c| *c.borrow_mut() = Some(cb));
}

fn emit(name: &str, payload: &str) {
    EVENT_CB.with(|c| {
        if let Some(cb) = c.borrow().as_ref() {
            let obj = js_sys::Object::new();
            let _ = Reflect::set(&obj, &"name".into(), &JsValue::from_str(name));
            let _ = Reflect::set(&obj, &"payload".into(), &JsValue::from_str(payload));
            let _ = cb.call1(&JsValue::NULL, &obj);
        }
    });
}

/// Async handshake driven entirely by the host's transport object.
///
/// `transport` is a plain JS object:
///   { remoteStaticKey(): Uint8Array, send(bytes: Uint8Array): void,
///     recv(): Promise<Uint8Array> }
///
/// This is the exact seam Kotlin/Wasm will have to implement in T3: it proves
/// that Rust can `await` a JS Promise without any IO inside the Wasm module.
#[wasm_bindgen]
pub async fn handshake(transport: JsValue, psk_hex: Option<String>) -> Result<String, JsValue> {
    let get = |name: &str| -> Result<JsValue, JsValue> {
        Reflect::get(&transport, &JsValue::from_str(name))
            .map_err(|_| err(format!("transport.{name} missing")))
    };
    let send: Function = get("send")?.dyn_into().map_err(|_| err("transport.send is not a function"))?;
    let recv: Function = get("recv")?.dyn_into().map_err(|_| err("transport.recv is not a function"))?;
    let key_fn: Function = get("remoteStaticKey")?
        .dyn_into()
        .map_err(|_| err("transport.remoteStaticKey is not a function"))?;

    let remote = Uint8Array::new(&key_fn.call0(&JsValue::NULL)?).to_vec();
    let mut peer = Peer::new(Role::Initiator, Some(remote), psk_hex.clone())?;
    let local_static = peer.static_public_key_hex();

    let msg1 = peer.write_handshake()?;
    send.call1(&JsValue::NULL, &Uint8Array::from(&msg1[..]))?;

    let reply = recv.call0(&JsValue::NULL)?;
    let reply: Promise = reply.dyn_into().map_err(|_| err("transport.recv did not return a Promise"))?;
    let msg2 = JsFuture::from(reply).await?;
    let msg2 = Uint8Array::new(&msg2).to_vec();
    peer.read_handshake(&msg2)?;

    if !peer.is_established() {
        return Err(err("handshake did not reach transport mode"));
    }

    let info = format!(
        "{{\"ok\":true,\"pattern\":\"{}\",\"local_static\":\"{}\",\"handshake_bytes\":{}}}",
        if psk_hex.is_some() { "IKpsk2" } else { "IK" },
        local_static,
        msg1.len() + msg2.len()
    );

    SESSION.with(|s| *s.borrow_mut() = peer.ts.take());
    emit("handshake.done", &info);
    Ok(info)
}

/// Encrypt one frame with the session established by `handshake`.
#[wasm_bindgen]
pub fn encrypt(plaintext: &[u8]) -> Result<Vec<u8>, JsValue> {
    SESSION.with(|s| {
        let mut slot = s.borrow_mut();
        let ts = slot.as_mut().ok_or_else(|| err("no session; call handshake() first"))?;
        let mut buf = vec![0u8; plaintext.len() + 16];
        let n = ts.write_message(plaintext, &mut buf).map_err(err)?;
        buf.truncate(n);
        Ok(buf)
    })
}

/// Decrypt one frame and surface it to the host as a `frame.received` event.
#[wasm_bindgen]
pub fn decrypt(ciphertext: &[u8]) -> Result<Vec<u8>, JsValue> {
    SESSION.with(|s| {
        let mut slot = s.borrow_mut();
        let ts = slot.as_mut().ok_or_else(|| err("no session; call handshake() first"))?;
        let mut buf = vec![0u8; ciphertext.len()];
        let n = ts.read_message(ciphertext, &mut buf).map_err(|e| {
            set_error(&format!("decrypt failed: {e}"));
            err(format!("decrypt failed: {e}"))
        })?;
        buf.truncate(n);
        emit("frame.received", &format!("{{\"bytes\":{}}}", n));
        Ok(buf)
    })
}

/// Last failure detail, for the host's diagnostics. Never contains key material.
#[wasm_bindgen]
pub fn last_error() -> Option<String> {
    LAST_ERROR.with(|c| c.borrow().clone())
}

/// Round-trip proof inside the module: IK handshake + AEAD both directions.
/// Returns a JSON string. Used by `cargo test`-less smoke tests and by T4 timing.
#[wasm_bindgen]
pub fn selftest() -> Result<String, JsValue> {
    fn msg(e: JsValue) -> String {
        e.as_string().unwrap_or_else(|| "non-string error".into())
    }
    let mut a = Peer::new(Role::Responder, None, None)?;
    let mut b = Peer::new(Role::Initiator, Some(a.local_static_pub.clone()), None)?;
    let m1 = b.write_handshake().map_err(|e| err(format!("step m1: {}", msg(e))))?;
    a.read_handshake(&m1)
        .map_err(|e| err(format!("step a.read(m1 len={}): {}", m1.len(), msg(e))))?;
    let m2 = a.write_handshake().map_err(|e| err(format!("step m2: {}", msg(e))))?;
    b.read_handshake(&m2)
        .map_err(|e| err(format!("step b.read(m2 len={}): {}", m2.len(), msg(e))))?;
    let ct = b.encrypt(b"vade-spike")?;
    let pt = a.decrypt(&ct)?;
    let ct2 = a.encrypt(b"pong")?;
    let pt2 = b.decrypt(&ct2)?;
    Ok(format!(
        "{{\"ok\":{},\"roundtrip\":\"{}\",\"pong\":\"{}\",\"ct_len\":{}}}",
        pt == b"vade-spike" && pt2 == b"pong",
        String::from_utf8_lossy(&pt),
        String::from_utf8_lossy(&pt2),
        ct.len()
    ))
}
