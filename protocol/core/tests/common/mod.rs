//! Shared helpers for the integration tests.
//!
//! Not a test target on its own (`tests/common/mod.rs` is a module).

#![allow(dead_code)]

use vade_protocol::handshake::{prologue, ConnectionRole, Handshake, Mode, Role};
use vade_protocol::identity::Identity;
use vade_protocol::transport::Transport;

/// Two fixed identities so failures are reproducible.
pub fn module_id() -> Identity {
    Identity::from_secret_bytes(&[0x22u8; 32]).expect("module identity")
}

pub fn core_id() -> Identity {
    Identity::from_secret_bytes(&[0x11u8; 32]).expect("core identity")
}

/// Run a full IK handshake: module initiates (it dialled), core responds.
pub fn ik_pair(module: &Identity, core: &Identity) -> (Transport, Transport) {
    ik_pair_result(module, core).expect("IK handshake must succeed")
}

/// Same, but with independent prologues so a mismatch can be provoked.
pub fn ik_pair_with_connection(
    module: &Identity,
    core: &Identity,
    module_conn: ConnectionRole,
    core_conn: ConnectionRole,
) -> Result<(Transport, Transport), vade_protocol::ProtocolError> {
    let module_pub = module.public_key_array();
    let core_pub = core.public_key_array();

    let mut m = Handshake::new(
        Mode::Ik,
        Role::Initiator,
        module_conn,
        module,
        Some(&core_pub),
        None,
    )?;
    let mut c = Handshake::new(
        Mode::Ik,
        Role::Responder,
        core_conn,
        core,
        Some(&module_pub),
        None,
    )?;
    let m1 = m.write(&[])?;
    c.read(&m1)?;
    let m2 = c.write(&[])?;
    m.read(&m2)?;
    Ok((m.into_transport()?, c.into_transport()?))
}

pub fn ik_pair_result(
    module: &Identity,
    core: &Identity,
) -> Result<(Transport, Transport), vade_protocol::ProtocolError> {
    ik_pair_with_connection(
        module,
        core,
        ConnectionRole::ModuleInitiated,
        ConnectionRole::ModuleInitiated,
    )
}

/// Run a full XXpsk3 pairing: 3 messages, PSK from the pair code.
pub fn xx_pair(
    module: &Identity,
    core: &Identity,
    psk: [u8; 32],
) -> Result<(Transport, Transport), vade_protocol::ProtocolError> {
    xx_pair_with_psk(module, core, &psk, &psk)
}

/// Same, but each side may use a different PSK (for the mismatch test).
pub fn xx_pair_with_psk(
    module: &Identity,
    core: &Identity,
    module_psk: &[u8],
    core_psk: &[u8],
) -> Result<(Transport, Transport), vade_protocol::ProtocolError> {
    let core_pub = core.public_key_array();
    let module_pub = module.public_key_array();

    let mut m = Handshake::new(
        Mode::XxPsk,
        Role::Initiator,
        ConnectionRole::ModuleInitiated,
        module,
        Some(&core_pub),
        Some(module_psk),
    )?;
    let mut c = Handshake::new(
        Mode::XxPsk,
        Role::Responder,
        ConnectionRole::ModuleInitiated,
        core,
        Some(&module_pub),
        Some(core_psk),
    )?;
    // -> e
    let m1 = m.write(&[])?;
    c.read(&m1)?;
    // <- e, ee, s, es
    let m2 = c.write(&[])?;
    m.read(&m2)?;
    // -> s, se, psk
    let m3 = m.write(&[])?;
    c.read(&m3)?;

    if !m.is_finished() || !c.is_finished() {
        return Err(vade_protocol::ProtocolError::new(
            vade_protocol::ErrorCode::Internal,
            "XXpsk3 did not complete after three messages",
        ));
    }
    Ok((m.into_transport()?, c.into_transport()?))
}

/// One request/response round trip through a pair of transports.
pub fn roundtrip(
    a: &mut Transport,
    b: &mut Transport,
    typ: vade_protocol::frame::FrameType,
    payload: &[u8],
) -> Vec<u8> {
    let frame = a.seal_frame(typ, payload).expect("seal");
    let wire = frame.encode();
    let decoded = vade_protocol::frame::Frame::decode(&wire, &a.limits()).expect("decode");
    b.open_frame(&decoded).expect("open")
}

/// Both prologues for a connection, for contract assertions.
pub fn both_prologues() -> (Vec<u8>, Vec<u8>) {
    (
        prologue(ConnectionRole::CoreInitiated),
        prologue(ConnectionRole::ModuleInitiated),
    )
}
