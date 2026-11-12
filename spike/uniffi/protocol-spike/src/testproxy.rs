//! Test-only TCP proxy: records the bytes on the wire and can flip exactly one
//! byte inside a chosen client→server frame. Used to prove two things without
//! trusting the implementation:
//!   * the plaintext never appears in the bytes on the wire;
//!   * flipping one ciphertext byte (body or Poly1305 tag) makes the handshake
//!     peer reject the frame.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

#[derive(Clone, Copy)]
pub enum Offset {
    FromStart(usize),
    FromEnd(usize),
}

pub struct Proxy {
    pub addr: String,
    pub c2s: Arc<Mutex<Vec<u8>>>,
    pub s2c: Arc<Mutex<Vec<u8>>>,
    stop: Arc<AtomicBool>,
    conns: Arc<AtomicUsize>,
}

impl Drop for Proxy {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
    }
}

impl Proxy {
    pub fn spawn(upstream: &str, tamper: Option<(usize, Offset)>) -> std::io::Result<Proxy> {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let addr = listener.local_addr()?.to_string();
        let c2s = Arc::new(Mutex::new(Vec::new()));
        let s2c = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let conns = Arc::new(AtomicUsize::new(0));

        {
            let upstream = upstream.to_string();
            let c2s = c2s.clone();
            let s2c = s2c.clone();
            let stop = stop.clone();
            let conns = conns.clone();
            std::thread::spawn(move || {
                for incoming in listener.incoming() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    let Ok(client) = incoming else { continue };
                    conns.fetch_add(1, Ordering::SeqCst);
                    let Ok(server) = TcpStream::connect(&upstream) else {
                        continue;
                    };
                    let (c2s, s2c) = (c2s.clone(), s2c.clone());
                    std::thread::spawn(move || {
                        let (mut cr, mut cw) = (client.try_clone().unwrap(), client);
                        let (mut sr, mut sw) = (server.try_clone().unwrap(), server);
                        let c2s_log = c2s.clone();
                        let up = std::thread::spawn(move || {
                            forward_c2s(&mut cr, &mut sw, &c2s_log, tamper);
                            let _ = sw.shutdown(std::net::Shutdown::Write);
                        });
                        let down = std::thread::spawn(move || {
                            forward_plain(&mut sr, &mut cw, &s2c);
                            let _ = cw.shutdown(std::net::Shutdown::Write);
                        });
                        let _ = up.join();
                        let _ = down.join();
                    });
                }
            });
        }

        Ok(Proxy {
            addr,
            c2s,
            s2c,
            stop,
            conns,
        })
    }

    pub fn bytes_c2s(&self) -> Vec<u8> {
        self.c2s.lock().unwrap().clone()
    }

    pub fn bytes_s2c(&self) -> Vec<u8> {
        self.s2c.lock().unwrap().clone()
    }
}

/// Parse length-prefixed frames on the client→server direction, optionally flip
/// one byte in `frame_index`, and forward complete frames.
fn forward_c2s(
    from: &mut TcpStream,
    to: &mut TcpStream,
    log: &Arc<Mutex<Vec<u8>>>,
    tamper: Option<(usize, Offset)>,
) {
    let mut pending: Vec<u8> = Vec::new();
    let mut frame_index = 0usize;
    let mut flipped = false;
    let mut chunk = [0u8; 4096];
    loop {
        let n = match from.read(&mut chunk) {
            Ok(0) | Err(_) => return,
            Ok(n) => n,
        };
        log.lock().unwrap().extend_from_slice(&chunk[..n]);
        pending.extend_from_slice(&chunk[..n]);

        loop {
            if pending.len() < 4 {
                break;
            }
            let len = u32::from_be_bytes([pending[0], pending[1], pending[2], pending[3]]) as usize;
            if pending.len() < 4 + len {
                break;
            }
            let mut frame: Vec<u8> = pending.drain(..4 + len).collect();
            if let Some((target, offset)) = tamper {
                if !flipped && frame_index == target {
                    let pos = match offset {
                        Offset::FromStart(o) => 4 + o,
                        Offset::FromEnd(o) => frame.len() - 1 - o,
                    };
                    if pos < frame.len() {
                        frame[pos] ^= 0x01;
                        flipped = true;
                    }
                }
            }
            frame_index += 1;
            if to.write_all(&frame).is_err() {
                return;
            }
            let _ = to.flush();
        }
    }
}

fn forward_plain(from: &mut TcpStream, to: &mut TcpStream, log: &Arc<Mutex<Vec<u8>>>) {
    let mut chunk = [0u8; 4096];
    loop {
        match from.read(&mut chunk) {
            Ok(0) | Err(_) => return,
            Ok(n) => {
                log.lock().unwrap().extend_from_slice(&chunk[..n]);
                if to.write_all(&chunk[..n]).is_err() {
                    return;
                }
                let _ = to.flush();
            }
        }
    }
}
