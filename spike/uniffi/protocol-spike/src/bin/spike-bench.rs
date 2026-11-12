//! Timing harness for RESULT.md: 20 handshakes (median), message round trips,
//! and the cost of the `Arc<Mutex<Session>>` layer the FFI design needs.

use std::sync::{Arc, Mutex};
use std::time::Instant;

use protocol_spike::{client, keys, server};

fn median(mut v: Vec<f64>) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}

fn p95(v: &[f64]) -> f64 {
    let mut s = v.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let idx = ((s.len() as f64) * 0.95).ceil() as usize - 1;
    s[idx.min(s.len() - 1)]
}

fn main() {
    let params = client::params().expect("params");
    let keypair = || snow::Builder::new(params.clone()).generate_keypair().unwrap();
    let s = keypair();
    let c = keypair();

    let srv = server::start_server(&s.private, &c.public, server::responder_prologue())
        .expect("start server");

    let mut hs_ms = Vec::new();
    let mut last_session = None;
    for _ in 0..20 {
        let mut sk = c.private.clone();
        let (session, report) = client::connect(
            &srv.addr,
            &sk,
            &s.public,
            &client::prologue(1),
        )
        .expect("handshake");
        keys::wipe(&mut sk);
        hs_ms.push(report.handshake_ms);
        last_session = Some(Arc::new(session));
    }
    let session = last_session.expect("at least one session");

    // Warm up, then measure 100 round trips of a 64-byte payload.
    let payload = vec![0x41u8; 64];
    let _ = session.round_trip(&payload).expect("warmup");
    let mut rt_ms = Vec::new();
    for _ in 0..100 {
        let t = Instant::now();
        let reply = session.round_trip(&payload).expect("round trip");
        assert_eq!(reply.len(), payload.len());
        rt_ms.push(t.elapsed().as_secs_f64() * 1000.0);
    }

    // Arc<Mutex<Session>> overhead: clone + lock/unlock + a 64-byte buffer copy,
    // i.e. everything the FFI layer adds on top of the Noise call itself.
    let m = Arc::new(Mutex::new(vec![0u8; 65535]));
    let n = 200_000;
    let t = Instant::now();
    for _ in 0..n {
        let a = m.clone();
        let mut g = a.lock().unwrap();
        g[..64].copy_from_slice(&payload);
        std::hint::black_box(&g[..64]);
    }
    let lock_ns = t.elapsed().as_nanos() as f64 / n as f64;

    println!(
        "{}",
        serde_json::json!({
            "handshakes": 20,
            "handshake_ms_median": median(hs_ms.clone()),
            "handshake_ms_min": hs_ms.iter().cloned().fold(f64::INFINITY, f64::min),
            "handshake_ms_max": hs_ms.iter().cloned().fold(0.0, f64::max),
            "roundtrips": 100,
            "roundtrip_ms_median": median(rt_ms.clone()),
            "roundtrip_ms_p95": p95(&rt_ms),
            "arc_mutex_copy_ns_per_op": lock_ns,
            "payload_bytes": 64,
            "server_handshakes": srv.handshakes(),
            "server_echoes": srv.echoes(),
            "lib_bytes": std::fs::metadata(std::env::current_exe().unwrap()).map(|m| m.len()).unwrap_or(0),
        })
    );
}
