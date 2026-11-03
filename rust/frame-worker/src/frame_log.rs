//! R3 frame recording (v2 spec D7). Measurement only: nothing here is read back
//! by a decision, and nothing here performs I/O until [`write_once`], which the
//! live binary calls once after the measurement and the report are finished.
//!
//! Each frame yields a receiver record (arrival, read statistics, receiver-side
//! drop stage) and, if the worker took it, a worker record (begun, finished,
//! published, compute, stage). Both go to preallocated fixed-capacity buffers
//! (`Ring`); overflow drops the newest record and is counted. [`render`] joins
//! the two by `(boot, generation, sequence)` into JSON lines.
use crate::pipeline::Clock;
use std::{
    env::VarError,
    fmt::Write as _,
    fs::OpenOptions,
    io::{self, Write as _},
    path::{Component, Path, PathBuf},
};

pub const ENV: &str = "ESP32CAM_RUST_TRIAL_FRAME_LOG";
pub const NOMINAL_INTERVAL_US: u64 = 200_000;
const PATH_LIMIT: usize = 4096;

/// Strict validation of the environment value. Unset is disabled (the existing
/// behavior). A value is accepted only for the finite continuous live trial:
/// an absolute, normalized path of a file that does not exist yet, in an
/// existing directory.
pub fn parse_env(
    setting: Result<String, VarError>,
    continuous: bool,
) -> Result<Option<PathBuf>, &'static str> {
    let value = match setting {
        Err(VarError::NotPresent) => return Ok(None),
        Err(VarError::NotUnicode(_)) => return Err("invalid frame log path"),
        Ok(value) => value,
    };
    if !continuous {
        return Err("frame log requires continuous policy");
    }
    if value.is_empty()
        || value.len() > PATH_LIMIT
        || value.ends_with('/')
        || value.chars().any(char::is_control)
    {
        return Err("invalid frame log path");
    }
    let path = Path::new(&value);
    // `components()` hides `.` and doubled slashes, so look at the text.
    if value
        .split('/')
        .skip(1)
        .any(|part| matches!(part, "" | "." | ".."))
        || !path.is_absolute()
        || path
            .components()
            .any(|c| !matches!(c, Component::RootDir | Component::Normal(_)))
        || path.file_name().is_none()
    {
        return Err("invalid frame log path");
    }
    let parent = path.parent().ok_or("invalid frame log path")?;
    if !std::fs::metadata(parent).is_ok_and(|m| m.is_dir()) {
        return Err("frame log directory unavailable");
    }
    match std::fs::symlink_metadata(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Some(path.to_path_buf())),
        _ => Err("frame log path exists"),
    }
}

/// Fixed-capacity buffer, allocated once. A full buffer drops the newest record.
pub struct Ring<T> {
    items: Vec<T>,
    capacity: usize,
    lost: u64,
}
impl<T> Ring<T> {
    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            items: Vec::with_capacity(capacity),
            capacity,
            lost: 0,
        }
    }
    /// Index of the stored record, or None when it was counted as lost.
    #[inline]
    pub fn push(&mut self, item: T) -> Option<usize> {
        if self.items.len() == self.capacity {
            self.lost = self.lost.saturating_add(1);
            return None;
        }
        self.items.push(item);
        Some(self.items.len() - 1)
    }
    pub fn items(&self) -> &[T] {
        &self.items
    }
    pub fn lost(&self) -> u64 {
        self.lost
    }
    pub fn capacity(&self) -> usize {
        self.capacity
    }
    #[cfg(test)]
    pub(crate) fn clear(&mut self) {
        self.items.clear();
    }
}
impl<T> std::ops::Index<usize> for Ring<T> {
    type Output = T;
    fn index(&self, i: usize) -> &T {
        &self.items[i]
    }
}
impl<T> std::ops::IndexMut<usize> for Ring<T> {
    fn index_mut(&mut self, i: usize) -> &mut T {
        &mut self.items[i]
    }
}

/// `TCP_INFO` values at the moment the frame completed. Linux only.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TcpSnapshot {
    pub total_retrans: u64,
    pub retrans: u64,
    pub lost: u64,
    pub rto_us: u64,
    pub rcv_rtt_us: u64,
    pub at_us: u64,
}

/// Read statistics of the socket reads that completed one message.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ReadSummary {
    pub first_byte_us: Option<u64>,
    pub last_byte_us: Option<u64>,
    pub reads: u32,
    pub max_read_us: u64,
    pub read_timeouts: u32,
    pub tcp: Option<TcpSnapshot>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadKind {
    Data,
    Timeout,
    Other,
}

/// Accumulates read statistics for the message being received. Passive: it only
/// observes results the reader already has and never changes a read.
pub struct Probe {
    clock: Clock,
    current: ReadSummary,
    last: ReadSummary,
}
impl Probe {
    pub fn new(clock: Clock) -> Self {
        Self {
            clock,
            current: ReadSummary::default(),
            last: ReadSummary::default(),
        }
    }
    /// `elapsed_us` is the duration of the read call. The read that delivers the
    /// first byte also waited for the camera, so it does not enter `max_read_us`,
    /// and timeouts before the first byte are idle waiting, not a stall.
    #[inline]
    pub fn note_read(&mut self, elapsed_us: u64, kind: ReadKind) {
        let c = &mut self.current;
        match kind {
            ReadKind::Data => {
                let now = (self.clock)();
                if c.first_byte_us.is_none() {
                    c.first_byte_us = Some(now);
                } else {
                    c.max_read_us = c.max_read_us.max(elapsed_us);
                }
                c.last_byte_us = Some(now);
                c.reads = c.reads.saturating_add(1);
            }
            ReadKind::Timeout if c.first_byte_us.is_some() => {
                c.max_read_us = c.max_read_us.max(elapsed_us);
                c.read_timeouts = c.read_timeouts.saturating_add(1);
            }
            _ => {}
        }
    }
    /// A message completed: publish the statistics and start a new message.
    pub fn complete(&mut self, tcp: Option<TcpSnapshot>) {
        self.last = std::mem::take(&mut self.current);
        self.last.tcp = tcp;
    }
    pub fn last(&self) -> ReadSummary {
        self.last
    }
    pub fn now(&self) -> u64 {
        (self.clock)()
    }
}

#[derive(Clone, Copy)]
pub struct RecvRecord {
    pub boot: u64,
    pub generation: u64,
    pub sequence: u64,
    pub captured_us: u64,
    pub sent_us: u64,
    pub received_us: u64,
    pub bytes: usize,
    pub read: ReadSummary,
    /// Receiver-side outcome (`order`, `mode`, `age_limit`, `clock_unavailable`,
    /// `busy`); None for a frame that was handed to the worker.
    pub stage: Option<&'static str>,
}

#[derive(Clone, Copy)]
pub struct WorkRecord {
    pub boot: u64,
    pub generation: u64,
    pub sequence: u64,
    pub begun_us: Option<u64>,
    pub finished_us: Option<u64>,
    pub published_us: Option<u64>,
    pub compute_us: Option<u64>,
    pub stage: &'static str,
}

fn num(out: &mut String, value: Option<u64>) {
    match value {
        Some(v) => {
            let _ = write!(out, "{v}");
        }
        None => out.push_str("null"),
    }
}

/// Join the receiver and worker records into the `frames.jsonl` content.
/// A frame handed to the worker without a worker record is `overwritten` if a
/// later frame was worked on (it was replaced in the one-slot queue), otherwise
/// `unprocessed` (still queued or in flight at the end).
pub fn render(recv: &Ring<RecvRecord>, work: &Ring<WorkRecord>) -> Vec<u8> {
    use std::collections::HashMap;
    let worked: HashMap<(u64, u64, u64), &WorkRecord> = work
        .items()
        .iter()
        .map(|w| ((w.boot, w.generation, w.sequence), w))
        .collect();
    let mut out = String::with_capacity(256 * (recv.items().len() + 1));
    let _ = writeln!(
        out,
        "{{\"v\":1,\"clock\":\"native_monotonic_us\",\"nominal_interval_us\":{NOMINAL_INTERVAL_US},\"capacity\":{},\"records\":{},\"lost_records\":{}}}",
        recv.capacity(),
        recv.items().len(),
        recv.lost().saturating_add(work.lost())
    );
    let mut later_worked = false;
    let mut lines = Vec::with_capacity(recv.items().len());
    for r in recv.items().iter().rev() {
        let w = worked.get(&(r.boot, r.generation, r.sequence)).copied();
        let w = w.filter(|_| r.stage.is_none());
        let stage = match (r.stage, w) {
            (Some(stage), _) => stage,
            (None, Some(w)) => w.stage,
            (None, None) if later_worked => "overwritten",
            (None, None) => "unprocessed",
        };
        later_worked |= w.is_some();
        let mut line = String::with_capacity(256);
        let _ = write!(
            line,
            "{{\"sequence\":{},\"boot_id\":{},\"generation\":{},\"first_byte_us\":",
            r.sequence, r.boot, r.generation
        );
        num(&mut line, r.read.first_byte_us);
        line.push_str(",\"last_byte_us\":");
        num(&mut line, r.read.last_byte_us);
        let _ = write!(
            line,
            ",\"bytes\":{},\"received_us\":{},\"captured_us\":{},\"sent_us\":{},\"begun_us\":",
            r.bytes, r.received_us, r.captured_us, r.sent_us
        );
        let field = |f: fn(&WorkRecord) -> Option<u64>| w.and_then(f);
        num(&mut line, field(|w| w.begun_us));
        line.push_str(",\"finished_us\":");
        num(&mut line, field(|w| w.finished_us));
        line.push_str(",\"published_us\":");
        num(&mut line, field(|w| w.published_us));
        line.push_str(",\"compute_us\":");
        num(&mut line, field(|w| w.compute_us));
        let _ = write!(
            line,
            ",\"stage\":\"{stage}\",\"reads\":{},\"max_read_us\":{},\"read_timeouts\":{},\"tcp\":",
            r.read.reads, r.read.max_read_us, r.read.read_timeouts
        );
        match r.read.tcp {
            Some(t) => {
                let _ = write!(
                    line,
                    "{{\"total_retrans\":{},\"retrans\":{},\"lost\":{},\"rto_us\":{},\"rcv_rtt_us\":{},\"at_us\":{}}}",
                    t.total_retrans, t.retrans, t.lost, t.rto_us, t.rcv_rtt_us, t.at_us
                );
            }
            None => line.push_str("null"),
        }
        line.push_str("}\n");
        lines.push(line);
    }
    for line in lines.iter().rev() {
        out.push_str(line);
    }
    out.into_bytes()
}

/// One write of the whole content to a new file (`O_EXCL`, mode 0400).
pub fn write_once(path: &Path, content: &[u8]) -> io::Result<()> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o400);
    options.open(path)?.write_all(content)
}

#[cfg(test)]
mod tests;
