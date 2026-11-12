//! Minimal length-prefixed framing for the spike.
//!
//! Deliberately *not* the C-01 frame format: this spike only needs to move one
//! Noise message per frame so the demo can prove an encrypted round trip.
//! 4-byte big-endian length + payload, hard cap 1 MiB.

use std::io::{self, Read, Write};

pub const MAX_FRAME: usize = 1 << 20;

/// A Noise handshake/transport message is far below this; the buffer is allocated once.
pub const MAX_NOISE_MSG: usize = 65535;

pub fn write_frame<W: Write>(w: &mut W, payload: &[u8]) -> io::Result<()> {
    if payload.len() > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "frame exceeds MAX_FRAME",
        ));
    }
    w.write_all(&(payload.len() as u32).to_be_bytes())?;
    w.write_all(payload)?;
    w.flush()
}

pub fn read_frame<R: Read>(r: &mut R) -> io::Result<Vec<u8>> {
    let mut len = [0u8; 4];
    r.read_exact(&mut len)?;
    let len = u32::from_be_bytes(len) as usize;
    if len > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "frame exceeds MAX_FRAME",
        ));
    }
    let mut buf = vec![0u8; len];
    r.read_exact(&mut buf)?;
    Ok(buf)
}

pub async fn write_frame_async<W>(w: &mut W, payload: &[u8]) -> io::Result<()>
where
    W: tokio::io::AsyncWrite + Unpin,
{
    use tokio::io::AsyncWriteExt;
    if payload.len() > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "frame exceeds MAX_FRAME",
        ));
    }
    w.write_all(&(payload.len() as u32).to_be_bytes()).await?;
    w.write_all(payload).await?;
    w.flush().await
}

pub async fn read_frame_async<R>(r: &mut R) -> io::Result<Vec<u8>>
where
    R: tokio::io::AsyncRead + Unpin,
{
    use tokio::io::AsyncReadExt;
    let mut len = [0u8; 4];
    r.read_exact(&mut len).await?;
    let len = u32::from_be_bytes(len) as usize;
    if len > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "frame exceeds MAX_FRAME",
        ));
    }
    let mut buf = vec![0u8; len];
    r.read_exact(&mut buf).await?;
    Ok(buf)
}
