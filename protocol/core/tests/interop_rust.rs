//! Happy-path integration tests: real handshakes, real ciphertext, no mocks.

mod common;

use vade_protocol::error::ErrorCode;
use vade_protocol::frame::{Frame, FrameType, Limits, MAX_NOISE_PAYLOAD};
use vade_protocol::paircode::derive_pair_psk;
use vade_protocol::transport::Transport;

#[test]
fn ik_handshake_then_bidirectional_traffic() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());

    assert!(m.is_initiator());
    assert!(!c.is_initiator());
    // Both sides agree on the handshake hash — the session id.
    assert_eq!(m.handshake_hash(), c.handshake_hash());
    assert_eq!(m.handshake_hash().len(), 32);
    assert_eq!(m.send_generation(), 1);
    assert_eq!(c.recv_generation(), 1);

    for i in 0..100u32 {
        let payload = format!("request-{i}").into_bytes();
        let frame = m.seal_frame(FrameType::Rpc, &payload).unwrap();
        assert_eq!(frame.counter(), u64::from(i) + 1);
        let wire = frame.encode();
        let decoded = Frame::decode(&wire, &Limits::default()).unwrap();
        assert_eq!(c.open_frame(&decoded).unwrap(), payload);

        let reply = format!("response-{i}").into_bytes();
        let frame = c.seal_frame(FrameType::Rpc, &reply).unwrap();
        assert_eq!(m.open_frame(&frame).unwrap(), reply);
    }
    assert_eq!(m.send_counter(), 100);
    assert_eq!(c.recv_counter(), 100);
    assert_eq!(c.send_counter(), 100);
    assert_eq!(m.recv_counter(), 100);
}

#[test]
fn ik_is_one_rtt() {
    let module = common::module_id();
    let core = common::core_id();
    let core_pub = core.public_key_array();
    let module_pub = module.public_key_array();

    let mut m = vade_protocol::handshake::Handshake::new(
        vade_protocol::handshake::Mode::Ik,
        vade_protocol::handshake::Role::Initiator,
        vade_protocol::handshake::ConnectionRole::ModuleInitiated,
        &module,
        Some(&core_pub),
        None,
    )
    .unwrap();
    let mut c = vade_protocol::handshake::Handshake::new(
        vade_protocol::handshake::Mode::Ik,
        vade_protocol::handshake::Role::Responder,
        vade_protocol::handshake::ConnectionRole::ModuleInitiated,
        &core,
        Some(&module_pub),
        None,
    )
    .unwrap();

    let msg1 = m.write(&[]).unwrap();
    assert!(!m.is_finished());
    c.read(&msg1).unwrap();
    // The responder only completes when it writes msg2.
    assert!(!c.is_finished());
    let msg2 = c.write(&[]).unwrap();
    assert!(c.is_finished());
    m.read(&msg2).unwrap();
    assert!(m.is_finished(), "IK completes for the initiator after msg2");

    // The responder learned the initiator's static key from msg1.
    assert_eq!(c.remote_static().unwrap(), module_pub.to_vec());
    assert_eq!(m.remote_static().unwrap(), core_pub.to_vec());
    assert!(c.remote_verified());
    assert!(m.remote_verified());
}

#[test]
fn xxpsk3_pairing_establishes_a_session() {
    let module = common::module_id();
    let core = common::core_id();
    let psk = derive_pair_psk("青松-A7K2M9", &core.public_key()).unwrap();
    let (mut m, mut c) = common::xx_pair(&module, &core, psk).unwrap();

    assert_eq!(m.handshake_hash(), c.handshake_hash());
    assert_eq!(
        c.remote_static().unwrap(),
        module.public_key(),
        "XX reveals the module key so core can register it"
    );
    let payload = b"pairing-register-frame".to_vec();
    let frame = m.seal_frame(FrameType::Rpc, &payload).unwrap();
    assert_eq!(c.open_frame(&frame).unwrap(), payload);
}

#[test]
fn fullwidth_pair_code_pairs_the_same_as_ascii() {
    let module = common::module_id();
    let core = common::core_id();
    let ascii = derive_pair_psk("青松-A7K2M9", &core.public_key()).unwrap();
    let fullwidth = derive_pair_psk("青松－Ａ７Ｋ２Ｍ９", &core.public_key()).unwrap();
    assert_eq!(ascii, fullwidth);
    let (mut m, mut c) = common::xx_pair(&module, &core, fullwidth).unwrap();
    let frame = m.seal_frame(FrameType::Rpc, b"ok").unwrap();
    assert_eq!(c.open_frame(&frame).unwrap(), b"ok");
}

#[test]
fn rekey_moves_only_the_sending_direction() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());

    let f1 = m.seal_frame(FrameType::Rpc, b"before").unwrap();
    c.open_frame(&f1).unwrap();

    let rekey = m.begin_rekey().unwrap();
    assert_eq!(rekey.typ(), FrameType::Rekey);
    assert_eq!(rekey.generation(), 1, "rekey frame travels under the OLD key");
    assert_eq!(m.send_generation(), 2);
    assert_eq!(m.send_counter(), 0, "counter restarts after rekey");

    let body = c.open_frame(&rekey).unwrap();
    assert_eq!(body, 2u32.to_be_bytes());
    assert_eq!(c.recv_generation(), 2);
    assert_eq!(c.recv_counter(), 0);

    // Core's own sending direction did not move.
    assert_eq!(c.send_generation(), 1);
    let back = c.seal_frame(FrameType::Rpc, b"reply").unwrap();
    assert_eq!(back.generation(), 1);
    assert_eq!(m.open_frame(&back).unwrap(), b"reply");

    // New generation, counter 1 again.
    let after = m.seal_frame(FrameType::Rpc, b"after").unwrap();
    assert_eq!(after.generation(), 2);
    assert_eq!(after.counter(), 1);
    assert_eq!(c.open_frame(&after).unwrap(), b"after");
}

#[test]
fn simultaneous_rekey_is_idempotent() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    // Both sides hit the 24h threshold at once.
    let r_module = m.begin_rekey().unwrap();
    let r_core = c.begin_rekey().unwrap();
    assert_eq!(m.send_generation(), 2);
    assert_eq!(c.send_generation(), 2);

    // Each side opens the other's request; neither may disconnect.
    assert_eq!(c.open_frame(&r_module).unwrap(), 2u32.to_be_bytes());
    assert_eq!(m.open_frame(&r_core).unwrap(), 2u32.to_be_bytes());
    assert_eq!(c.recv_generation(), 2);
    assert_eq!(m.recv_generation(), 2);

    let f = m.seal_frame(FrameType::Rpc, b"still alive").unwrap();
    assert_eq!(c.open_frame(&f).unwrap(), b"still alive");
}

#[test]
fn stale_rekey_request_is_ignored_not_fatal() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let r1 = m.begin_rekey().unwrap();
    c.open_frame(&r1).unwrap(); // receive generation is 2 now
    assert_eq!(c.recv_generation(), 2);

    // A duplicate request announcing the generation we already applied.
    let stale = m.seal_frame(FrameType::Rekey, &2u32.to_be_bytes()).unwrap();
    let out = c.open_frame(&stale).unwrap();
    assert_eq!(u32::from_be_bytes([out[0], out[1], out[2], out[3]]), 2);
    assert_eq!(c.recv_generation(), 2, "ignored, key unchanged");

    // Still usable in both directions afterwards.
    let f = m.seal_frame(FrameType::Rpc, b"after stale").unwrap();
    assert_eq!(c.open_frame(&f).unwrap(), b"after stale");
}

#[test]
fn large_frame_roundtrip() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    // One Noise message carries at most 65535 bytes including the 16-byte tag.
    let payload = vec![0xA5u8; MAX_NOISE_PAYLOAD as usize];
    let frame = m.seal_frame(FrameType::Rpc, &payload).unwrap();
    assert_eq!(frame.payload_len(), MAX_NOISE_PAYLOAD);
    let wire = frame.encode();
    assert_eq!(wire.len(), 20 + MAX_NOISE_PAYLOAD as usize + 16);
    let decoded = Frame::decode(&wire, &Limits::default()).unwrap();
    assert_eq!(c.open_frame(&decoded).unwrap(), payload);

    // One byte more is refused before any crypto runs.
    let too_big = vec![0u8; MAX_NOISE_PAYLOAD as usize + 1];
    let e = m.seal_frame(FrameType::Rpc, &too_big).unwrap_err();
    assert_eq!(e.code(), ErrorCode::PayloadTooLarge, "{e}");
}

#[test]
fn directional_counters_are_independent() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    // Interleave five in each direction; both counters advance independently.
    for i in 0..5u64 {
        let a = m.seal_frame(FrameType::Rpc, b"m").unwrap();
        assert_eq!(a.counter(), i + 1);
        c.open_frame(&a).unwrap();
        let b = c.seal_frame(FrameType::Event, b"c").unwrap();
        assert_eq!(b.counter(), i + 1);
        m.open_frame(&b).unwrap();
    }
    assert_eq!(m.send_counter(), 5);
    assert_eq!(m.recv_counter(), 5);

    // A clone of the transport state does not exist; the only way to keep the
    // counters aligned across reconnects is a fresh handshake, as designed.
    let _: &Transport = &m;
}

#[test]
fn empty_payloads_are_legal() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let frame = m.seal_frame(FrameType::Heartbeat, b"").unwrap();
    assert_eq!(frame.payload_len(), 0);
    assert_eq!(frame.body().len(), 16, "empty plaintext still carries a tag");
    assert!(c.open_frame(&frame).unwrap().is_empty());
}
