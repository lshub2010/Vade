//! `cargo bench --bench aead` — AEAD cost per 1 KiB message (NFR-2: P95 < 2 ms
//! on this machine) plus a handshake timing sanity check (NFR-3: < 50 ms).
//!
//! Plain std timing, no criterion: the numbers only need to be honest and
//! reproducible, and an extra dependency on an embedded box is not free.

use std::time::Instant;

use vade_protocol::frame::{Frame, FrameType};
use vade_protocol::handshake::{ConnectionRole, Handshake, Mode, Role};
use vade_protocol::identity::Identity;

fn percentile(sorted_ns: &[u64], p: f64) -> u64 {
    if sorted_ns.is_empty() {
        return 0;
    }
    let idx = ((sorted_ns.len() as f64 - 1.0) * p).round() as usize;
    sorted_ns[idx]
}

fn pair() -> (vade_protocol::transport::Transport, vade_protocol::transport::Transport) {
    let module = Identity::from_secret_bytes(&[0x22u8; 32]).unwrap();
    let core = Identity::from_secret_bytes(&[0x11u8; 32]).unwrap();
    let core_pub = core.public_key_array();
    let module_pub = module.public_key_array();
    let mut m = Handshake::new(
        Mode::Ik,
        Role::Initiator,
        ConnectionRole::ModuleInitiated,
        &module,
        Some(&core_pub),
        None,
    )
    .unwrap();
    let mut c = Handshake::new(
        Mode::Ik,
        Role::Responder,
        ConnectionRole::ModuleInitiated,
        &core,
        Some(&module_pub),
        None,
    )
    .unwrap();
    let m1 = m.write(&[]).unwrap();
    c.read(&m1).unwrap();
    let m2 = c.write(&[]).unwrap();
    m.read(&m2).unwrap();
    (m.into_transport().unwrap(), c.into_transport().unwrap())
}

fn handshake_micros(n: usize) -> Vec<u64> {
    let module = Identity::from_secret_bytes(&[0x22u8; 32]).unwrap();
    let core = Identity::from_secret_bytes(&[0x11u8; 32]).unwrap();
    let core_pub = core.public_key_array();
    let module_pub = module.public_key_array();
    let mut samples = Vec::with_capacity(n);
    for _ in 0..n {
        let t = Instant::now();
        let mut m = Handshake::new(
            Mode::Ik,
            Role::Initiator,
            ConnectionRole::ModuleInitiated,
            &module,
            Some(&core_pub),
            None,
        )
        .unwrap();
        let mut c = Handshake::new(
            Mode::Ik,
            Role::Responder,
            ConnectionRole::ModuleInitiated,
            &core,
            Some(&module_pub),
            None,
        )
        .unwrap();
        let m1 = m.write(&[]).unwrap();
        c.read(&m1).unwrap();
        let m2 = c.write(&[]).unwrap();
        m.read(&m2).unwrap();
        let _ = m.into_transport().unwrap();
        let _ = c.into_transport().unwrap();
        samples.push(t.elapsed().as_micros() as u64);
    }
    samples.sort_unstable();
    samples
}

fn main() {
    let (mut m, mut c) = pair();
    let plaintext = vec![0x5Au8; 1024];
    let iterations = 5_000u64;

    // Warm up (page faults, branch predictors, cipher init).
    for _ in 0..200 {
        let f = m.seal_frame(FrameType::Rpc, &plaintext).unwrap();
        let wire = f.encode();
        let decoded = Frame::decode(&wire, &Default::default()).unwrap();
        assert_eq!(c.open_frame(&decoded).unwrap(), plaintext);
    }

    let mut seal_ns = Vec::with_capacity(iterations as usize);
    let mut open_ns = Vec::with_capacity(iterations as usize);
    let mut rtt_ns = Vec::with_capacity(iterations as usize);

    for _ in 0..iterations {
        let t = Instant::now();
        let f = m.seal_frame(FrameType::Rpc, &plaintext).unwrap();
        seal_ns.push(t.elapsed().as_nanos() as u64);

        let wire = f.encode();
        let decoded = Frame::decode(&wire, &Default::default()).unwrap();

        let t = Instant::now();
        let out = c.open_frame(&decoded).unwrap();
        open_ns.push(t.elapsed().as_nanos() as u64);
        assert_eq!(out.len(), plaintext.len());

        rtt_ns.push(seal_ns.last().unwrap() + open_ns.last().unwrap());
    }

    seal_ns.sort_unstable();
    open_ns.sort_unstable();
    rtt_ns.sort_unstable();

    println!("iterations            = {iterations} (1 KiB payload, one direction)");
    println!(
        "seal  p50/p95/p99 ms  = {:.3} / {:.3} / {:.3}",
        percentile(&seal_ns, 0.50) as f64 / 1e6,
        percentile(&seal_ns, 0.95) as f64 / 1e6,
        percentile(&seal_ns, 0.99) as f64 / 1e6
    );
    println!(
        "open  p50/p95/p99 ms  = {:.3} / {:.3} / {:.3}",
        percentile(&open_ns, 0.50) as f64 / 1e6,
        percentile(&open_ns, 0.95) as f64 / 1e6,
        percentile(&open_ns, 0.99) as f64 / 1e6
    );
    println!(
        "seal+open p95 ms      = {:.3}",
        percentile(&rtt_ns, 0.95) as f64 / 1e6
    );

    let hs = handshake_micros(200);
    println!(
        "handshake p50/p95 ms  = {:.2} / {:.2}",
        percentile(&hs, 0.50) as f64 / 1e3,
        percentile(&hs, 0.95) as f64 / 1e3
    );

    let open_p95_ms = percentile(&open_ns, 0.95) as f64 / 1e6;
    let seal_p95_ms = percentile(&seal_ns, 0.95) as f64 / 1e6;
    println!(
        "NFR-2 (P95 < 2 ms)    = {}",
        if open_p95_ms < 2.0 && seal_p95_ms < 2.0 {
            "PASS"
        } else {
            "FAIL"
        }
    );
    println!(
        "NFR-3 (P95 < 50 ms)   = {}",
        if (percentile(&hs, 0.95) as f64 / 1e3) < 50.0 {
            "PASS"
        } else {
            "FAIL"
        }
    );
}
