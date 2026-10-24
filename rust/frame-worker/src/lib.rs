//! Bounded ECF envelopes. Authentication, JPEG validation and clock/ACK gates
//! belong to the caller; decoding an envelope never establishes freshness.
#![forbid(unsafe_code)]

use std::collections::VecDeque;
use std::io::{self, Read};
use std::sync::Mutex;

pub mod clock;
pub mod detector;
#[cfg(feature = "jpeg")]
pub mod jpeg;
pub mod operating_mode;
pub mod replay;
#[cfg(feature = "transport")]
pub mod transport;

pub const MAX_JPEG: usize = 128 * 1024;
pub const MAX_GENERATION: u64 = (1 << 53) - 1;

#[derive(Debug, PartialEq, Eq)]
pub struct Frame {
    pub boot: u64,
    pub sequence: u64,
    pub captured_us: u64,
    pub sent_us: u64,
    pub generation: Option<u64>,
    pub jpeg: Vec<u8>,
}

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid ECF envelope")
}

fn header_size(magic: &[u8], profiles: bool) -> io::Result<usize> {
    match magic {
        b"ECF1" => Ok(40),
        b"ECF2" if profiles => Ok(48),
        _ => Err(invalid()),
    }
}

pub fn decode(bytes: &[u8], profiles: bool) -> io::Result<Frame> {
    let header = header_size(bytes.get(..4).ok_or_else(invalid)?, profiles)?;
    if bytes.len() <= header || bytes.len() > header + MAX_JPEG {
        return Err(invalid());
    }
    let number = |offset| u64::from_be_bytes(bytes[offset..offset + 8].try_into().unwrap());
    let (boot, sequence, captured_us, sent_us) = (number(4), number(12), number(20), number(28));
    let generation = (header == 48).then(|| number(36));
    let length = u32::from_be_bytes(bytes[header - 4..header].try_into().unwrap()) as usize;
    if boot == 0
        || sequence == 0
        || captured_us == 0
        || sent_us < captured_us
        || generation.is_some_and(|g| g == 0 || g > MAX_GENERATION)
        || length != bytes.len() - header
    {
        return Err(invalid());
    }
    Ok(Frame {
        boot,
        sequence,
        captured_us,
        sent_us,
        generation,
        jpeg: bytes[header..].to_vec(),
    })
}

/// Replay stream reader. EOF is clean only between complete envelopes.
/// A declared JPEG length is checked before any payload-sized allocation.
pub fn read_frame(reader: &mut impl Read, profiles: bool) -> io::Result<Option<Frame>> {
    let mut header = [0u8; 48];
    loop {
        match reader.read(&mut header[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    reader.read_exact(&mut header[1..4])?;
    let size = header_size(&header[..4], profiles)?;
    reader.read_exact(&mut header[4..size])?;
    let length = u32::from_be_bytes(header[size - 4..size].try_into().unwrap()) as usize;
    if !(1..=MAX_JPEG).contains(&length) {
        return Err(invalid());
    }
    let mut bytes = Vec::with_capacity(size + length);
    bytes.extend_from_slice(&header[..size]);
    bytes.resize(size + length, 0);
    reader.read_exact(&mut bytes[size..])?;
    decode(&bytes, profiles).map(Some)
}

/// One owner serializes observations; rejected frames never mutate ordering.
#[derive(Default)]
pub struct FrameOrder {
    boot: Option<u64>,
    sequence: u64,
    captured_us: u64,
    retired: VecDeque<u64>,
}

impl FrameOrder {
    pub fn observe(&mut self, frame: &Frame) -> Result<(&'static str, u64), &'static str> {
        let changed = self.boot != Some(frame.boot);
        let kind = if changed && self.boot.is_some() {
            "boot_changed"
        } else {
            "new"
        };
        if changed && self.retired.contains(&frame.boot) {
            return Err("retired_boot");
        }
        if !changed {
            if frame.sequence == self.sequence {
                return Err("duplicate");
            }
            if frame.sequence < self.sequence || frame.captured_us <= self.captured_us {
                return Err("stale");
            }
        }
        let gap = if changed {
            0
        } else {
            frame.sequence - self.sequence - 1
        };
        if changed && let Some(old) = self.boot {
            if self.retired.len() == 32 {
                self.retired.pop_front();
            }
            self.retired.push_back(old);
        }
        self.boot = Some(frame.boot);
        self.sequence = frame.sequence;
        self.captured_us = frame.captured_us;
        Ok((kind, gap))
    }
}

/// At most one waiting value. Producers never wait for a busy/poisoned lock.
pub struct LatestSlot<T>(Mutex<Option<T>>);

impl<T> Default for LatestSlot<T> {
    fn default() -> Self {
        Self(Mutex::new(None))
    }
}

impl<T> LatestSlot<T> {
    pub fn replace(&self, item: T) -> Result<Option<T>, T> {
        match self.0.try_lock() {
            Ok(mut slot) => Ok(slot.replace(item)),
            Err(_) => Err(item),
        }
    }

    pub fn take(&self) -> Option<T> {
        self.0.try_lock().ok()?.take()
    }
}

#[cfg(test)]
mod tests;
