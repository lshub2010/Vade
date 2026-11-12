//! Crate-level tests: real Noise IK over TCP, wire-level tamper, callback events.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use snow::{Builder, HandshakeState};

use crate::client::{self, Session};
use crate::testproxy::{Offset, Proxy};
use crate::{server, EventSink};

fn keypair() -> (Vec<u8>, Vec<u8>) {
    let params = client::params().expect("params");
    let kp = Builder::new(params).generate_keypair().expect("keygen");
    (kp.private, kp.public)
}

fn connect(
    addr: &str,
    client_sk: &[u8],
    server_pub: &[u8],
) -> Result<(Session, client::HandshakeReport), crate::SpikeError> {
    client::connect(addr, client_sk, server_pub, &client::default_prologue())
}

#[test]
fn version_matches_crate_version() {
    assert_eq!(crate::version(), env!("CARGO_PKG_VERSION"));
    assert!(!crate::version().is_empty());
}

#[test]
fn handshake_and_encrypted_echo_over_tcp() {
    let (s_sk, s_pub) = keypair();
    let (c_sk, c_pub) = keypair();
    let srv = server::start_server(&s_sk, &c_pub, server::responder_prologue()).expect("server");
    let (session, report) = connect(&srv.addr, &c_sk, &s_pub).expect("handshake");

    // IK is 1-RTT: msg1 (e, es, s, ss) = 32 + 48 + 16 = 96 bytes,
    // msg2 (e, ee, se) = 32 + 16 = 48 bytes, plus a 4-byte length prefix each.
    assert_eq!(report.handshake_wire_bytes, 4 + 96 + 4 + 48);
    assert_eq!(report.remote_static, s_pub);
    assert_eq!(report.handshake_hash.len(), 32);
    assert!(report.handshake_ms >= 0.0);

    let reply = session.round_trip(b"hello vade").expect("round trip");
    assert_eq!(reply, b"hello vade");
    assert!(wait_until(|| srv.handshakes() == 1), "server did not count the handshake");
    assert!(wait_until(|| srv.echoes() == 1), "server did not count the echo");
    assert_eq!(srv.auth_failures(), 0);
}

#[test]
fn plaintext_never_appears_on_the_wire() {
    let (s_sk, s_pub) = keypair();
    let (c_sk, c_pub) = keypair();
    let srv = server::start_server(&s_sk, &c_pub, server::responder_prologue()).expect("server");
    let proxy = Proxy::spawn(&srv.addr, None).expect("proxy");

    let (session, _report) = connect(&proxy.addr, &c_sk, &s_pub).expect("handshake");
    let plaintext = b"VADE-SPIKE-PLAINTEXT-MARKER-0123456789";
    let reply = session.round_trip(plaintext).expect("round trip");
    assert_eq!(reply, plaintext);

    let c2s = proxy.bytes_c2s();
    let s2c = proxy.bytes_s2c();
    assert!(c2s.len() >= plaintext.len() + 16 + 4, "c2s too short");
    assert!(
        !contains(&c2s, plaintext),
        "plaintext leaked in client->server bytes"
    );
    assert!(
        !contains(&s2c, plaintext),
        "plaintext leaked in server->client bytes"
    );
    // The transport frame is exactly header + plaintext + 16-byte Poly1305 tag.
    let last = last_frame(&c2s).expect("a frame");
    assert_eq!(last.len(), plaintext.len() + 16);
}

#[test]
fn tampered_ciphertext_body_is_rejected() {
    tamper_case(Offset::FromStart(5), "body");
}

#[test]
fn tampered_auth_tag_is_rejected() {
    tamper_case(Offset::FromEnd(1), "tag");
}

fn tamper_case(offset: Offset, label: &str) {
    let (s_sk, s_pub) = keypair();
    let (c_sk, c_pub) = keypair();
    let srv = server::start_server(&s_sk, &c_pub, server::responder_prologue()).expect("server");
    // Frame 1 on the client->server direction is the first transport frame
    // (0 = IK msg1; IK is 1-RTT, so there is no msg3).
    let proxy = Proxy::spawn(&srv.addr, Some((1, offset))).expect("proxy");

    let (session, _report) = connect(&proxy.addr, &c_sk, &s_pub).expect("handshake");
    let err = session
        .round_trip(b"tampered payload")
        .expect_err(&format!("tampered {label} byte must not be accepted"));
    let code = err.code();
    assert!(
        code == "UNAVAILABLE" || code == "TIMEOUT" || code == "AUTH_FAILED",
        "unexpected code {code}: {err}"
    );
    assert_eq!(srv.handshakes(), 1);
    assert_eq!(srv.echoes(), 0, "server must not echo a tampered frame");
    assert_eq!(srv.auth_failures(), 1, "server must record an auth failure");
}

#[test]
fn in_process_tamper_fails_authentication() {
    // Direct handshake pair, no sockets: flip one byte of the ciphertext and of
    // the tag, and require `snow` to reject both.
    let (init, resp) = handshake_pair();
    let mut init = init.into_transport_mode().unwrap();
    let mut resp = resp.into_transport_mode().unwrap();
    let mut out = [0u8; 512];
    let mut scratch = [0u8; 512];
    let n = init.write_message(b"one two three", &mut out).unwrap();
    let mut wire = out[..n].to_vec();

    let mut body = wire.clone();
    body[3] ^= 0x01;
    let err = resp.read_message(&body, &mut scratch).unwrap_err();
    assert_eq!(client::map_snow(err).code(), "AUTH_FAILED");

    wire[n - 1] ^= 0x01; // Poly1305 tag byte
    let err = resp.read_message(&wire, &mut scratch).unwrap_err();
    assert_eq!(client::map_snow(err).code(), "AUTH_FAILED");

    // A failed read advances the receiver's nonce, so that session is dead by
    // design: the only safe reaction is to tear the connection down (C1 rule).
    // A fresh pair still works:
    let (init2, resp2) = handshake_pair();
    let mut init2 = init2.into_transport_mode().unwrap();
    let mut resp2 = resp2.into_transport_mode().unwrap();
    let n = init2.write_message(b"second message", &mut out).unwrap();
    let k = resp2.read_message(&out[..n], &mut scratch).unwrap();
    assert_eq!(&scratch[..k], b"second message");
}

#[test]
fn unknown_but_completable_client_key_is_caught_by_whitelist() {
    // Proves the explicit whitelist check in the server is load-bearing: a
    // client whose key the server does not accept must not get a session.
    let (s_sk, s_pub) = keypair();
    let (c_sk, _c_pub) = keypair();
    let (_x, other_pub) = keypair();
    let srv = server::start_server(&s_sk, &other_pub, server::responder_prologue()).unwrap();
    let err = connect(&srv.addr, &c_sk, &s_pub).expect_err("not whitelisted");
    assert_eq!(err.code(), "UNAVAILABLE");
    assert_eq!(srv.auth_failures(), 1);
    assert_eq!(srv.handshakes(), 0);
}

#[test]
fn wrong_pinned_server_key_is_rejected() {
    let (s_sk, _s_pub) = keypair();
    let (c_sk, c_pub) = keypair();
    let (_other_sk, other_pub) = keypair();
    let srv = server::start_server(&s_sk, &c_pub, server::responder_prologue()).expect("server");

    // Client whitelists a key the server does not hold. The server cannot
    // decrypt msg1, so it drops the connection and the client sees EOF.
    let err = connect(&srv.addr, &c_sk, &other_pub).expect_err("impostor key must fail");
    assert!(
        err.code() == "AUTH_FAILED" || err.code() == "UNAVAILABLE",
        "unexpected code {}",
        err.code()
    );
    assert_eq!(srv.handshakes(), 0);
}

#[test]
fn prologue_mismatch_is_rejected() {
    let (s_sk, s_pub) = keypair();
    let (c_sk, c_pub) = keypair();
    // Server is configured for a module-initiated connection while the client
    // uses the core-initiated prologue -> different hash -> fall over.
    let srv = server::start_server(&s_sk, &c_pub, client::prologue(client::ROLE_MODULE_INITIATES))
        .expect("server");

    let err = connect(&srv.addr, &c_sk, &s_pub).expect_err("prologue mismatch must fail");
    assert!(
        err.code() == "AUTH_FAILED" || err.code() == "UNAVAILABLE",
        "unexpected code {}",
        err.code()
    );
    assert_eq!(srv.handshakes(), 0);
}

#[test]
fn bad_address_is_a_config_error() {
    let (c_sk, _c_pub) = keypair();
    let (_s_sk, s_pub) = keypair();
    let err = connect("not-an-address", &c_sk, &s_pub).expect_err("bad addr");
    assert_eq!(err.code(), "INVALID_ARGUMENT");

    let err = connect("127.0.0.1:1", &c_sk, &s_pub).expect_err("nothing listening");
    assert_eq!(err.code(), "UNAVAILABLE");
}

#[test]
fn twenty_sequential_handshakes_all_succeed() {
    let (s_sk, s_pub) = keypair();
    let (c_sk, c_pub) = keypair();
    let srv = server::start_server(&s_sk, &c_pub, server::responder_prologue()).expect("server");
    for i in 0..20 {
        let (session, _) = connect(&srv.addr, &c_sk, &s_pub)
            .unwrap_or_else(|e| panic!("handshake {i} failed: {e}"));
        let reply = session.round_trip(format!("n={i}").as_bytes()).unwrap();
        assert_eq!(reply, format!("n={i}").as_bytes());
    }
    assert_eq!(srv.handshakes(), 20);
    assert_eq!(srv.auth_failures(), 0);
}

// ---------------------------------------------------------------------------
// Callback interface
// ---------------------------------------------------------------------------

struct CaptureSink {
    events: Arc<Mutex<Vec<String>>>,
    calls: Arc<AtomicUsize>,
}

impl EventSink for CaptureSink {
    fn on_event(&self, json: String) {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.events.lock().unwrap().push(json);
    }
}

#[test]
fn event_sink_receives_ordered_events() {
    let events_buf = Arc::new(Mutex::new(Vec::new()));
    let calls = Arc::new(AtomicUsize::new(0));
    crate::set_event_sink(Box::new(CaptureSink {
        events: events_buf.clone(),
        calls: calls.clone(),
    }));

    let (s_sk, s_pub) = keypair();
    let (c_sk, c_pub) = keypair();
    let srv = server::start_server(&s_sk, &c_pub, server::responder_prologue()).expect("server");

    // Drive the real exported entry points, with keys supplied through the
    // documented env channel (single test in this process -> no env races).
    let dir = std::env::temp_dir().join(format!("vade-spike-test-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sk_path = dir.join("client_static.sk.hex");
    crate::keys::write_secret_file(&sk_path, &crate::keys::hex_encode(&c_sk)).unwrap();
    std::env::set_var("VADE_SPIKE_SERVER_PUB", crate::keys::hex_encode(&s_pub));
    std::env::set_var("VADE_SPIKE_CLIENT_SK_FILE", &sk_path);

    let rt = crate::rt();
    let addr = srv.addr.clone();
    let body = rt.block_on(crate::handshake(addr.clone())).expect("handshake");
    assert!(body.contains("\"ok\":true"), "body: {body}");
    assert!(body.contains(client::PROTOCOL));

    let reply = rt
        .block_on(crate::send_encrypted(addr.clone(), b"via ffi".to_vec()))
        .expect("send");
    assert_eq!(reply, b"via ffi");

    // Missing session -> typed error with a C-04 code, not a stringly error.
    let err = rt
        .block_on(crate::send_encrypted("127.0.0.1:9".to_string(), vec![1]))
        .expect_err("unknown session");
    assert_eq!(err.code(), "NOT_FOUND");

    assert!(crate::close_session(addr.clone()));
    assert_eq!(crate::session_count(), 0);
    crate::clear_event_sink();

    let events = events_buf.lock().unwrap().clone();
    let kinds: Vec<String> = events
        .iter()
        .map(|e| {
            serde_json::from_str::<serde_json::Value>(e)
                .ok()
                .and_then(|v| v["event"].as_str().map(str::to_string))
                .unwrap_or_else(|| format!("unparsable:{e}"))
        })
        .collect();
    assert_eq!(
        kinds,
        vec![
            "session.state",
            "handshake.completed",
            "message.sent",
            "message.received",
            "session.closed"
        ],
        "events: {events:?}"
    );
    assert!(events.iter().all(|e| !e.contains("via ffi")));
    assert_eq!(calls.load(Ordering::SeqCst), 5);
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/// Poll a server-side counter for up to 2s (the server task is asynchronous).
fn wait_until(mut pred: impl FnMut() -> bool) -> bool {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while std::time::Instant::now() < deadline {
        if pred() {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    pred()
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

fn last_frame(bytes: &[u8]) -> Option<Vec<u8>> {
    let mut i = 0;
    let mut last = None;
    while i + 4 <= bytes.len() {
        let len = u32::from_be_bytes([bytes[i], bytes[i + 1], bytes[i + 2], bytes[i + 3]]) as usize;
        if i + 4 + len > bytes.len() {
            return None;
        }
        last = Some(bytes[i + 4..i + 4 + len].to_vec());
        i += 4 + len;
    }
    last
}

fn handshake_pair() -> (HandshakeState, HandshakeState) {
    let params = client::params().unwrap();
    let s = Builder::new(params.clone()).generate_keypair().unwrap();
    let c = Builder::new(params.clone()).generate_keypair().unwrap();
    let mut init = Builder::new(params.clone())
        .local_private_key(&c.private)
        .unwrap()
        .remote_public_key(&s.public)
        .unwrap()
        .prologue(&client::prologue(1))
        .unwrap()
        .build_initiator()
        .unwrap();
    let mut resp = Builder::new(params)
        .local_private_key(&s.private)
        .unwrap()
        .remote_public_key(&c.public)
        .unwrap()
        .prologue(&client::default_prologue())
        .unwrap()
        .build_responder()
        .unwrap();

    let mut a = [0u8; 512];
    let mut b = [0u8; 512];
    let n = init.write_message(&[], &mut a).unwrap();
    resp.read_message(&a[..n], &mut b).unwrap();
    let n = resp.write_message(&[], &mut a).unwrap();
    init.read_message(&a[..n], &mut b).unwrap();
    assert!(init.is_handshake_finished() && resp.is_handshake_finished());
    (init, resp)
}
