//! Strict firmware control messages and one outstanding clock exchange.
//! The owner must additionally gate hello, ACK, ordering, ownership and JPEGs.
use crate::{
    Frame, MAX_GENERATION,
    clock::{AgeBounds, ClockSample, ClockWindow},
    operating_mode::Mode,
};
use serde_json::{Map, Value, json};

pub const TEXT_LIMIT: usize = 512;
type Result<T> = std::result::Result<T, &'static str>;
#[derive(Debug, PartialEq, Eq)]
pub enum Control {
    Hello {
        boot: u64,
        floor: u64,
    },
    Ack {
        boot: u64,
        generation: u64,
        mode: Mode,
        applied_us: u64,
    },
    Pong {
        boot: u64,
        nonce: String,
        device_us: u64,
    },
    Status {
        boot: u64,
        counters: [u64; 6],
    },
}
impl Control {
    pub fn boot(&self) -> u64 {
        match self {
            Self::Hello { boot, .. }
            | Self::Ack { boot, .. }
            | Self::Pong { boot, .. }
            | Self::Status { boot, .. } => *boot,
        }
    }
}
fn fields(object: &Map<String, Value>, keys: &[&str]) -> bool {
    object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
}
fn uint(value: &Value, zero: bool) -> Option<u64> {
    value.as_u64().filter(|n| zero || *n > 0)
}
pub fn parse(raw: &str) -> Result<Control> {
    if raw.len() > TEXT_LIMIT {
        return Err("text_size");
    }
    let item: Value = serde_json::from_str(raw).map_err(|_| "invalid_json")?;
    let object = item.as_object().ok_or("invalid_text")?;
    let boot = uint(&item["boot_id"], false);
    match item["type"].as_str() {
        Some("profile.hello") => {
            let floor = uint(&item["generation_floor"], true).filter(|n| *n <= MAX_GENERATION);
            if !fields(
                object,
                &["type", "schema", "boot_id", "generation_floor", "profiles"],
            ) || item["schema"].as_u64() != Some(2)
                || item["profiles"] != json!(["eco", "tracking"])
                || boot.is_none()
                || floor.is_none()
            {
                return Err("invalid_profile_hello");
            }
            Ok(Control::Hello {
                boot: boot.unwrap(),
                floor: floor.unwrap(),
            })
        }
        Some("profile.ack") => {
            let generation = uint(&item["generation"], false).filter(|n| *n <= MAX_GENERATION);
            let applied = uint(&item["applied_us"], false);
            let mode = match item["mode"].as_str() {
                Some("eco") => Some(Mode::Eco),
                Some("tracking") => Some(Mode::Tracking),
                _ => None,
            };
            if !fields(
                object,
                &["type", "boot_id", "generation", "mode", "applied_us"],
            ) || boot.is_none()
                || generation.is_none()
                || applied.is_none()
                || mode.is_none()
            {
                return Err("invalid_profile_ack");
            }
            Ok(Control::Ack {
                boot: boot.unwrap(),
                generation: generation.unwrap(),
                mode: mode.unwrap(),
                applied_us: applied.unwrap(),
            })
        }
        Some("clock.pong") => {
            let nonce = item["id"].as_str().filter(|s| s.chars().count() == 16);
            let device = uint(&item["device_us"], false);
            if !fields(object, &["type", "id", "boot_id", "device_us"])
                || boot.is_none()
                || nonce.is_none()
                || device.is_none()
            {
                return Err("invalid_pong");
            }
            Ok(Control::Pong {
                boot: boot.unwrap(),
                nonce: nonce.unwrap().into(),
                device_us: device.unwrap(),
            })
        }
        Some("capture.status") => {
            let names = [
                "attempts",
                "captured",
                "capture_failures",
                "stale",
                "sent",
                "send_failures",
            ];
            let values: Option<Vec<_>> = names.iter().map(|key| uint(&item[key], true)).collect();
            if !fields(
                object,
                &[
                    "type",
                    "boot_id",
                    "attempts",
                    "captured",
                    "capture_failures",
                    "stale",
                    "sent",
                    "send_failures",
                ],
            ) || boot.is_none()
                || values.is_none()
            {
                return Err("invalid_status");
            }
            Ok(Control::Status {
                boot: boot.unwrap(),
                counters: values.unwrap().try_into().unwrap(),
            })
        }
        _ => Err("unknown_type"),
    }
}

#[derive(Default)]
pub struct SessionClock {
    boot: Option<u64>,
    pending: Option<(String, u64)>,
    last_ping: Option<u64>,
    window: ClockWindow,
}
impl SessionClock {
    pub fn bind(&mut self, boot: u64) -> Result<()> {
        if boot == 0 || self.boot.is_some_and(|b| b != boot) {
            return Err("boot_mismatch");
        }
        self.boot = Some(boot);
        Ok(())
    }
    pub fn poll(
        &mut self,
        now: u64,
        tracking: bool,
        nonce: impl FnOnce() -> Result<String>,
    ) -> Result<Option<Value>> {
        if now == 0 {
            return Err("invalid_host_clock");
        }
        if self
            .pending
            .as_ref()
            .is_some_and(|(_, sent)| now.checked_sub(*sent).is_none_or(|t| t > 1_000_000))
        {
            return Err("clock_timeout");
        }
        let interval = if tracking { 50_000 } else { 2_000_000 };
        let due = match self.last_ping {
            None => true,
            Some(last) => now.checked_sub(last).ok_or("invalid_host_clock")? >= interval,
        };
        if self.pending.is_some() || !due {
            return Ok(None);
        }
        let id = nonce()?;
        if id.len() != 16 || !id.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("invalid_nonce");
        }
        self.pending = Some((id.clone(), now));
        self.last_ping = Some(now);
        Ok(Some(json!({"type":"clock.ping","id":id})))
    }
    pub fn pong(&mut self, boot: u64, nonce: &str, device: u64, received: u64) -> Result<()> {
        let (expected, sent) = self.pending.as_ref().ok_or("clock_nonce")?;
        if nonce != expected {
            return Err("clock_nonce");
        }
        let sample = ClockSample::from_exchange(boot, device, *sent, received)?;
        self.bind(boot)?;
        self.window.add(sample)?;
        self.pending = None;
        Ok(())
    }
    /// Called only on rejection; it cannot change or renew clock evidence.
    pub fn diagnostic(&self, frame: &Frame, received: u64) -> (Option<AgeBounds>, Vec<[u64; 4]>) {
        (
            self.window.age_bounds(frame, received),
            self.window.evidence(),
        )
    }
    pub fn admit(&self, frame: &Frame, received: u64) -> Option<Arrival> {
        if self.boot != Some(frame.boot) {
            return None;
        }
        let bounds = self.window.age_bounds(frame, received)?;
        (bounds.1 <= 500_000).then_some(Arrival {
            received,
            bounds,
            identity: frame_identity(frame),
        })
    }
}

/// Frozen conservative age at arrival; newer samples never renew this evidence.
type FrameIdentity = (u64, u64, u64, u64, Option<u64>);
fn frame_identity(frame: &Frame) -> FrameIdentity {
    (
        frame.boot,
        frame.sequence,
        frame.captured_us,
        frame.sent_us,
        frame.generation,
    )
}
#[derive(Clone, Copy)]
pub struct Arrival {
    received: u64,
    bounds: AgeBounds,
    identity: FrameIdentity,
}
impl Arrival {
    pub fn received_us(&self) -> u64 {
        self.received
    }
    pub fn matches(&self, frame: &Frame) -> bool {
        self.identity == frame_identity(frame)
    }
    pub fn age_bounds(&self, frame: &Frame, now: u64) -> Option<AgeBounds> {
        if !self.matches(frame) {
            return None;
        }
        let elapsed = i128::from(now.checked_sub(self.received)?);
        Some((self.bounds.0 + elapsed, self.bounds.1 + elapsed))
    }
    pub fn fits(&self, frame: &Frame, now: u64, reserve_us: u64) -> bool {
        self.age_bounds(frame, now)
            .is_some_and(|b| b.1 + i128::from(reserve_us) <= 500_000)
    }
}

#[cfg(test)]
mod tests;
