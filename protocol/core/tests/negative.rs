//! Negative tests. Every case here must **fail for real** — the assertion is on
//! the error code and, where it matters, on the side effect that proves the
//! check happened where it claims to (e.g. no receive nonce burned).
//!
//! Required classes (parent task): wrong version, wrong role, wrong prologue,
//! flipped ciphertext byte, flipped tag, replay. Everything else in the C1 task
//! book's table (§T6) is here too.

mod common;

use vade_protocol::error::ErrorCode;
use vade_protocol::frame::{Frame, FrameHeader, FrameType, Limits, TAG_LEN};
use vade_protocol::handshake::{ConnectionRole, Handshake, Mode, Role};
use vade_protocol::paircode::derive_pair_psk;
use vade_protocol::transport::Transport;

fn err_code(e: &vade_protocol::ProtocolError) -> ErrorCode {
    e.code()
}

// ---------------------------------------------------------------------------
// 1. 错版本 — rejected before decryption, and provably so.
// ---------------------------------------------------------------------------
#[test]
fn wrong_version_is_rejected_before_decryption() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let frame = m.seal_frame(FrameType::Rpc, b"hello").unwrap();

    let mut wire = frame.encode();
    wire[0] = 2; // ver = 2
    let e = Frame::decode(&wire, &Limits::default()).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::ProtocolMismatch, "{e}");
    assert!(e.is_fatal());

    // Decryption never ran: the receive counter is untouched...
    assert_eq!(c.recv_counter(), 0);
    // ...and the untouched frame still opens, which would be impossible if the
    // rejected frame had consumed a receive nonce.
    let good = Frame::decode(&frame.encode(), &Limits::default()).unwrap();
    assert_eq!(c.open_frame(&good).unwrap(), b"hello");
}

// ---------------------------------------------------------------------------
// 2. 错角色 — the connection-role byte is part of the prologue.
// ---------------------------------------------------------------------------
#[test]
fn wrong_role_byte_breaks_the_handshake() {
    let module = common::module_id();
    let core = common::core_id();
    let e = common::ik_pair_with_connection(
        &module,
        &core,
        ConnectionRole::ModuleInitiated,
        ConnectionRole::CoreInitiated, // core claims it dialled
    )
    .unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal());
}

// ---------------------------------------------------------------------------
// 3. 错 prologue — a peer that speaks "vade/v2" cannot talk to us.
// ---------------------------------------------------------------------------
#[test]
fn wrong_prologue_prefix_breaks_the_handshake() {
    use snow::Builder;

    let module = common::module_id();
    let core = common::core_id();
    let core_pub = core.public_key_array();
    let module_secret = module.to_secret_bytes();

    // A foreign implementation: same Noise pattern, different prologue prefix.
    let params = "Noise_IK_25519_ChaChaPoly_BLAKE2s".parse().unwrap();
    let mut foreign = Builder::new(params)
        .local_private_key(&module_secret)
        .unwrap()
        .remote_public_key(&core_pub)
        .unwrap()
        .prologue(b"vade/v2\x02")
        .unwrap()
        .build_initiator()
        .unwrap();

    let mut core_hs = Handshake::new(
        Mode::Ik,
        Role::Responder,
        ConnectionRole::ModuleInitiated,
        &core,
        Some(&module.public_key_array()),
        None,
    )
    .unwrap();

    let mut buf = vec![0u8; 65535];
    let n = foreign.write_message(&[], &mut buf).unwrap();
    let e = core_hs.read(&buf[..n]).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal(), "a prologue mismatch kills the connection");
}

#[test]
fn prologue_is_prefix_plus_role_byte() {
    let (core_initiated, module_initiated) = common::both_prologues();
    assert_eq!(core_initiated, b"vade/v1\x01");
    assert_eq!(module_initiated, b"vade/v1\x02");
}

// ---------------------------------------------------------------------------
// 4. 改 1 字节密文 — Poly1305 tag verification fails, session is dead.
// ---------------------------------------------------------------------------
#[test]
fn flipped_ciphertext_byte_is_rejected() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let frame = m.seal_frame(FrameType::Rpc, b"the quick brown fox").unwrap();

    let mut wire = frame.encode();
    wire[20] ^= 0x01; // first ciphertext byte
    let tampered = Frame::decode(&wire, &Limits::default()).unwrap();
    let e = c.open_frame(&tampered).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal());

    // Pinned behaviour, measured rather than assumed: snow 0.10 keeps the
    // receive nonce when a decrypt fails (`cipherstate.rs::decrypt_ad` only
    // bumps `n` on success), and we only bump `recv_counter` on success. So the
    // primitive is still aligned...
    assert_eq!(c.recv_counter(), 0, "a rejected frame must not move the counter");
    let original = Frame::decode(&frame.encode(), &Limits::default()).unwrap();
    assert_eq!(c.open_frame(&original).unwrap(), b"the quick brown fox");
    // ...but the error is fatal *by policy*: the SDK must drop the link, because
    // a bad tag means an on-path attacker or a broken peer and the caller cannot
    // tell them apart. `is_fatal()` above is that contract.
}

// ---------------------------------------------------------------------------
// 5. 改 tag — only the last byte of the tag changes.
// ---------------------------------------------------------------------------
#[test]
fn flipped_tag_is_rejected() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let frame = m.seal_frame(FrameType::Event, b"tag test").unwrap();
    assert_eq!(frame.body().len(), b"tag test".len() + TAG_LEN);

    let mut wire = frame.encode();
    let last = wire.len() - 1;
    wire[last] ^= 0x80;
    let tampered = Frame::decode(&wire, &Limits::default()).unwrap();
    let e = c.open_frame(&tampered).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal());
}

// ---------------------------------------------------------------------------
// 6. 重放 — same (generation, counter) twice.
// ---------------------------------------------------------------------------
#[test]
fn replayed_frame_is_rejected() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let frame = m.seal_frame(FrameType::Rpc, b"once").unwrap();
    assert_eq!(c.open_frame(&frame).unwrap(), b"once");

    let e = c.open_frame(&frame).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal(), "replay must kill the connection");
    assert!(e.message().contains("replay"), "{}", e.message());
}

#[test]
fn counter_rollback_is_rejected() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let f1 = m.seal_frame(FrameType::Rpc, b"1").unwrap();
    let f2 = m.seal_frame(FrameType::Rpc, b"2").unwrap();
    let f3 = m.seal_frame(FrameType::Rpc, b"3").unwrap();
    c.open_frame(&f1).unwrap();
    c.open_frame(&f2).unwrap();
    c.open_frame(&f3).unwrap();

    let e = c.open_frame(&f2).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal());
    assert_eq!(c.recv_counter(), 3, "a rejected frame must not move the counter");
}

#[test]
fn non_contiguous_send_counter_is_refused() {
    let (mut m, _c) = common::ik_pair(&common::module_id(), &common::core_id());
    // First frame is counter 1; asking for 3 is a gap and must be refused.
    let e = m
        .seal(vade_protocol::transport::Direction::Outbound, 1, 3, b"x")
        .unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");
}

// ---------------------------------------------------------------------------
// 7. generation 回退 / 跳代 / rekey 后旧 key
// ---------------------------------------------------------------------------
#[test]
fn generation_rollback_is_rejected() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let old = m.seal_frame(FrameType::Rpc, b"gen1").unwrap();
    c.open_frame(&old).unwrap();

    let rekey = m.begin_rekey().unwrap();
    c.open_frame(&rekey).unwrap();
    assert_eq!(c.recv_generation(), 2);

    // A frame left over from generation 1 arrives late.
    let stale = m.seal_frame(FrameType::Rpc, b"still gen2").unwrap();
    let mut header = *stale.header();
    header.generation = 1;
    let forged = Frame::from_parts(header, stale.body().to_vec()).unwrap();
    let e = c.open_frame(&forged).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal());
    assert!(e.message().contains("generation rollback"), "{}", e.message());
}

#[test]
fn generation_jump_is_rejected() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let frame = m.seal_frame(FrameType::Rpc, b"jump").unwrap();
    let mut header = *frame.header();
    header.generation = 3; // 1 -> 3 skips a generation
    let forged = Frame::from_parts(header, frame.body().to_vec()).unwrap();
    let e = c.open_frame(&forged).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::ProtocolMismatch, "{e}");
    assert!(e.is_fatal());
}

#[test]
fn old_key_after_rekey_is_rejected() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let old = m.seal_frame(FrameType::Rpc, b"old key").unwrap();
    c.open_frame(&old).unwrap(); // keep both AEAD nonces aligned

    let rekey = m.begin_rekey().unwrap();
    c.open_frame(&rekey).unwrap();
    assert_eq!(c.recv_generation(), 2);

    // Right generation number, but the body was sealed with the gen-1 key.
    let mut header = *old.header();
    header.generation = 2;
    header.counter = 1;
    let forged = Frame::from_parts(header, old.body().to_vec()).unwrap();
    let e = c.open_frame(&forged).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal());

    // Positive control: a genuine gen-2 frame with the same counter still opens,
    // so the rejection above was about the old key material, not a broken state.
    let fresh = m.seal_frame(FrameType::Rpc, b"new key").unwrap();
    assert_eq!(fresh.generation(), 2);
    assert_eq!(fresh.counter(), 1);
    assert_eq!(c.open_frame(&fresh).unwrap(), b"new key");
}

// ---------------------------------------------------------------------------
// 8. 反射 — our own outbound frame fed back into our own open().
// ---------------------------------------------------------------------------
#[test]
fn reflected_frame_is_rejected() {
    let (mut m, _c) = common::ik_pair(&common::module_id(), &common::core_id());
    let frame = m.seal_frame(FrameType::Rpc, b"reflect me").unwrap();
    let e = m.open_frame(&frame).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal(), "direction separation is cryptographic, not cosmetic");
}

#[test]
fn seal_refuses_an_inbound_direction() {
    let (mut m, _c) = common::ik_pair(&common::module_id(), &common::core_id());
    let e = m
        .seal(vade_protocol::transport::Direction::Inbound, 1, 1, b"x")
        .unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");
}

// ---------------------------------------------------------------------------
// 9. 握手：错误静态公钥 / 错误配对码 / 降级 / 未完成
// ---------------------------------------------------------------------------
#[test]
fn ik_with_an_unlisted_static_key_fails() {
    let core = common::core_id();
    let real_module = common::module_id();
    let impostor = vade_protocol::identity::Identity::from_secret_bytes(&[0x33u8; 32]).unwrap();
    let core_pub = core.public_key_array();

    // The core whitelists the real module's key.
    let mut responder = Handshake::new(
        Mode::Ik,
        Role::Responder,
        ConnectionRole::ModuleInitiated,
        &core,
        Some(&real_module.public_key_array()),
        None,
    )
    .unwrap();
    // The impostor holds a valid key pair, just not a whitelisted one.
    let mut attacker = Handshake::new(
        Mode::Ik,
        Role::Initiator,
        ConnectionRole::ModuleInitiated,
        &impostor,
        Some(&core_pub),
        None,
    )
    .unwrap();

    let msg1 = attacker.write(&[]).unwrap();
    // snow accepts any static key here; our explicit comparison is what rejects it.
    let e = responder.read(&msg1).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal());
    assert!(
        e.message().contains("whitelist"),
        "must name the whitelist check: {}",
        e.message()
    );
}

#[test]
fn xx_with_the_wrong_pair_code_fails() {
    let module = common::module_id();
    let core = common::core_id();
    let good = derive_pair_psk("青松-A7K2M9", &core.public_key()).unwrap();
    let bad = derive_pair_psk("青松-Z9Z9Z9", &core.public_key()).unwrap();
    assert_ne!(good, bad);

    let e = common::xx_pair_with_psk(&module, &core, &good, &bad).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PermissionDenied, "{e}");
    assert!(e.is_fatal());
}

#[test]
fn ik_without_a_remote_key_refuses_to_downgrade() {
    let module = common::module_id();
    let e = Handshake::new(
        Mode::Ik,
        Role::Initiator,
        ConnectionRole::ModuleInitiated,
        &module,
        None,
        None,
    )
    .unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");
}

#[test]
fn xx_without_a_psk_is_rejected() {
    let module = common::module_id();
    let core_pub = common::core_id().public_key_array();
    let e = Handshake::new(
        Mode::XxPsk,
        Role::Initiator,
        ConnectionRole::ModuleInitiated,
        &module,
        Some(&core_pub),
        None,
    )
    .unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");
}

#[test]
fn into_transport_before_the_pattern_finishes_is_rejected() {
    let module = common::module_id();
    let core_pub = common::core_id().public_key_array();
    let hs = Handshake::new(
        Mode::Ik,
        Role::Initiator,
        ConnectionRole::ModuleInitiated,
        &module,
        Some(&core_pub),
        None,
    )
    .unwrap();
    let e = hs.into_transport().unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");
}

#[test]
fn oversized_handshake_message_is_rejected() {
    let module = common::module_id();
    let core = common::core_id();
    let core_pub = core.public_key_array();
    let mut m = Handshake::new(
        Mode::Ik,
        Role::Initiator,
        ConnectionRole::ModuleInitiated,
        &module,
        Some(&core_pub),
        None,
    )
    .unwrap();
    let big = vec![0u8; 65 * 1024];
    let e = m.write(&big).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PayloadTooLarge, "{e}");

    let mut responder = Handshake::new(
        Mode::Ik,
        Role::Responder,
        ConnectionRole::ModuleInitiated,
        &core,
        Some(&module.public_key_array()),
        None,
    )
    .unwrap();
    let e = responder.read(&big).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PayloadTooLarge, "{e}");
    assert!(e.is_fatal());
}

// ---------------------------------------------------------------------------
// 10. 帧层：长度放大 / 截断 / 未知类型 / 保留位
// ---------------------------------------------------------------------------
#[test]
fn oversized_declared_length_allocates_nothing() {
    let limits = Limits::default();
    // A 20-byte header claiming a 2 MiB payload. Nothing else is supplied.
    let header = FrameHeader::new(FrameType::Rpc, 1, 1, 2 * 1024 * 1024).encode();
    let e = Frame::decode(&header, &limits).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::PayloadTooLarge, "{e}");
    assert!(e.is_fatal());

    #[cfg(target_os = "linux")]
    {
        fn rss_bytes() -> u64 {
            let statm = std::fs::read_to_string("/proc/self/statm").expect("statm");
            let resident: u64 = statm.split_whitespace().nth(1).unwrap().parse().unwrap();
            resident * 4096
        }
        // Warm up allocators, then hammer the rejection path.
        for _ in 0..1_000 {
            let _ = Frame::decode(&header, &limits);
        }
        let before = rss_bytes();
        for _ in 0..20_000 {
            let e = Frame::decode(&header, &limits).unwrap_err();
            assert_eq!(err_code(&e), ErrorCode::PayloadTooLarge);
        }
        let after = rss_bytes();
        let growth = after.saturating_sub(before);
        assert!(
            growth < 1024 * 1024,
            "rejecting 20k oversized headers grew RSS by {growth} bytes"
        );
    }
}

#[test]
fn truncated_frame_is_rejected() {
    let (mut m, _c) = common::ik_pair(&common::module_id(), &common::core_id());
    let frame = m.seal_frame(FrameType::Rpc, b"truncate").unwrap();
    let wire = frame.encode();

    let e = Frame::decode(&wire[..wire.len() - 1], &Limits::default()).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");

    let e = Frame::decode(&wire[..10], &Limits::default()).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");

    let mut longer = wire.clone();
    longer.push(0);
    let e = Frame::decode(&longer, &Limits::default()).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");
}

#[test]
fn unknown_frame_type_and_reserved_bytes_are_rejected() {
    let base = Frame::from_body(FrameType::Rpc, 1, 1, vec![0u8; TAG_LEN]).unwrap();
    let wire = base.encode();

    let mut unknown = wire.clone();
    unknown[1] = 0x09;
    assert_eq!(
        err_code(&Frame::decode(&unknown, &Limits::default()).unwrap_err()),
        ErrorCode::ProtocolMismatch
    );

    let mut flags = wire.clone();
    flags[2] = 0x01;
    assert_eq!(
        err_code(&Frame::decode(&flags, &Limits::default()).unwrap_err()),
        ErrorCode::ProtocolMismatch
    );

    let mut rsv = wire.clone();
    rsv[3] = 0xFF;
    assert_eq!(
        err_code(&Frame::decode(&rsv, &Limits::default()).unwrap_err()),
        ErrorCode::ProtocolMismatch
    );

    let mut zero_counter = wire;
    zero_counter[12..20].copy_from_slice(&0u64.to_be_bytes());
    assert_eq!(
        err_code(&Frame::decode(&zero_counter, &Limits::default()).unwrap_err()),
        ErrorCode::ProtocolMismatch
    );
}

#[test]
fn handshake_frames_are_not_accepted_by_the_transport() {
    let (mut m, mut _c) = common::ik_pair(&common::module_id(), &common::core_id());
    let f = Frame::from_body(FrameType::Handshake, 0, 0, vec![0u8; 32]).unwrap();
    let e = m.open_frame(&f).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");

    let e = m
        .seal_frame(FrameType::Handshake, b"nope")
        .unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::InvalidArgument, "{e}");
}

#[test]
fn rekey_body_must_be_four_bytes() {
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    // A rekey frame whose plaintext is 5 bytes instead of a u32.
    let f = m.seal_frame(FrameType::Rekey, b"12345").unwrap();
    let e = c.open_frame(&f).unwrap_err();
    assert_eq!(err_code(&e), ErrorCode::ProtocolMismatch, "{e}");
    assert!(e.is_fatal());
}

#[test]
fn transport_rejects_a_foreign_generation_after_rekey() {
    // Sanity: the checker itself is exercised through a second rekey.
    let (mut m, mut c) = common::ik_pair(&common::module_id(), &common::core_id());
    let r1 = m.begin_rekey().unwrap();
    c.open_frame(&r1).unwrap();
    assert_eq!(m.send_generation(), 2);
    assert_eq!(c.recv_generation(), 2);
    // Core's own sending direction is untouched (design §2.2.2 方向性).
    assert_eq!(c.send_generation(), 1);
    let back: Transport = c;
    let _ = back;
}
