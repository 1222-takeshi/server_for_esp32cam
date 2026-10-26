//! One-camera acknowledged modes. The owner must serialize ALL operations,
//! including the bounded RAM-only publication callback, under the same mutex.
//! A trusted monotonic microsecond clock is required; no motor commands exist.
use crate::{Frame, MAX_GENERATION};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::panic::{AssertUnwindSafe, catch_unwind};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Eco,
    Tracking,
}
impl Mode {
    pub fn name(self) -> &'static str {
        match self {
            Self::Eco => "eco",
            Self::Tracking => "tracking",
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct ModeError(pub u16);
type Result<T> = std::result::Result<T, ModeError>;
struct Stamp {
    at: u64,
    captured: u64,
    sequence: u64,
}

pub struct OperatingMode {
    clock: Box<dyn Fn() -> u64 + Send + Sync>,
    version: u64,
    boot: Option<u64>,
    connected: bool,
    requested: Mode,
    state: &'static str,
    reason: Option<&'static str>,
    ack: Option<u64>,
    ack_at: Option<u64>,
    cadence_at: Option<u64>,
    cadence_count: u64,
    cadence_rate: Option<f64>,
    requested_at: u64,
    last_command: Option<u64>,
    frames: VecDeque<Stamp>,
    rejected_ack: u64,
    rejected_result: u64,
    transitions: u64,
    eco_recoverable: bool,
    recovery_ready: bool,
    recovery_valid_until: Option<u64>,
    recovery_guard: Option<Box<dyn Fn() -> bool + Send + Sync>>,
    eco_confirmed: bool,
    automatic_recoveries: u64,
    auto_recover_tracking: bool,
    tracking_recoverable: bool,
    tracking_hold_started: Option<u64>,
    last_tracking_hold: Option<(u64, u64)>,
    tracking_hold_total: u64,
    tracking_hold_events: u64,
    tracking_automatic_recoveries: u64,
}

impl OperatingMode {
    /// Runtime supplies a CSPRNG initial generation in 1..=MAX_GENERATION.
    pub fn new(version: u64, clock: impl Fn() -> u64 + Send + Sync + 'static) -> Result<Self> {
        Self::new_with_tracking_recovery(version, clock, false)
    }
    /// Opt-in policy fixed before connection. No physical motor control exists.
    pub fn new_with_tracking_recovery(
        version: u64,
        clock: impl Fn() -> u64 + Send + Sync + 'static,
        auto_recover_tracking: bool,
    ) -> Result<Self> {
        if !(1..=MAX_GENERATION).contains(&version) {
            return Err(ModeError(400));
        }
        let requested_at = clock();
        Ok(Self {
            clock: Box::new(clock),
            version,
            boot: None,
            connected: false,
            requested: Mode::Eco,
            state: "unknown",
            reason: Some("disconnected"),
            ack: None,
            ack_at: None,
            cadence_at: None,
            cadence_count: 0,
            cadence_rate: None,
            requested_at,
            last_command: None,
            frames: VecDeque::new(),
            rejected_ack: 0,
            rejected_result: 0,
            transitions: 0,
            eco_recoverable: false,
            recovery_ready: false,
            recovery_valid_until: None,
            recovery_guard: None,
            eco_confirmed: false,
            automatic_recoveries: 0,
            auto_recover_tracking,
            tracking_recoverable: false,
            tracking_hold_started: None,
            last_tracking_hold: None,
            tracking_hold_total: 0,
            tracking_hold_events: 0,
            tracking_automatic_recoveries: 0,
        })
    }
    fn now(&self) -> u64 {
        (self.clock)()
    }
    pub fn generation(&self) -> u64 {
        self.version
    }
    pub fn requested(&self) -> Mode {
        self.requested
    }
    pub fn connected(&self) -> bool {
        self.connected
    }
    /// For tracking recovery, true is a scoped permit, not a cached observation:
    /// the owner must hold its resource guard and validate current capture age
    /// throughout the recovery commit, then reset false before unlocking State.
    pub fn set_recovery_ready(&mut self, ready: bool) {
        self.recovery_ready = ready;
    }
    /// Owner's conservative resource/capture deadline, checked at commit time.
    pub fn set_recovery_valid_until(&mut self, deadline: Option<u64>) {
        self.recovery_valid_until = deadline;
    }
    /// Bounded owner check at commit, e.g. a supervisor wall-clock ticket.
    pub fn set_recovery_guard(&mut self, guard: Option<Box<dyn Fn() -> bool + Send + Sync>>) {
        self.recovery_guard = guard;
    }

    fn finish_tracking_hold(&mut self) {
        if let Some(start) = self.tracking_hold_started.take() {
            let end = self.now();
            self.last_tracking_hold = Some((start, end));
            self.tracking_hold_total = self
                .tracking_hold_total
                .saturating_add(end.saturating_sub(start));
        }
    }
    pub fn tracking_hold_started(&self) -> Option<u64> {
        self.tracking_hold_started
    }
    pub fn last_tracking_hold(&self) -> Option<(u64, u64)> {
        self.last_tracking_hold
    }
    fn change(&mut self, mode: Mode, reason: Option<&'static str>) -> Result<()> {
        self.finish_tracking_hold();
        self.tracking_recoverable = false;
        if self.version >= MAX_GENERATION {
            self.connected = false;
            self.state = "degraded";
            self.reason = Some("version_exhausted");
            self.ack = None;
            self.ack_at = None;
            self.frames.clear();
            return Err(ModeError(503));
        }
        self.version += 1;
        self.transitions = self.transitions.saturating_add(1);
        self.requested = mode;
        self.state = if reason.is_some() {
            "degraded"
        } else {
            "pending"
        };
        self.reason = reason;
        self.ack = None;
        self.ack_at = None;
        self.cadence_at = None;
        self.cadence_count = 0;
        self.cadence_rate = None;
        self.frames.clear();
        self.requested_at = self.now();
        self.last_command = None;
        self.eco_recoverable = false;
        self.eco_confirmed = false;
        Ok(())
    }
    pub fn hello(&mut self, boot: u64, floor: u64) -> Result<()> {
        if self.connected || boot == 0 || floor > MAX_GENERATION {
            return Err(ModeError(400));
        }
        self.version = self.version.max(floor);
        self.boot = Some(boot);
        self.connected = true;
        self.change(Mode::Eco, None)
    }
    pub fn disconnect(&mut self) -> Result<()> {
        if !self.connected && self.requested == Mode::Eco && self.ack.is_none() {
            self.state = "unknown";
            return Ok(());
        }
        self.finish_tracking_hold();
        self.tracking_recoverable = false;
        self.connected = false;
        if self.version < MAX_GENERATION {
            self.change(Mode::Eco, Some("disconnected"))?;
        } else {
            self.requested = Mode::Eco;
            self.reason = Some("version_exhausted");
            self.ack = None;
            self.ack_at = None;
            self.frames.clear();
        }
        self.state = "unknown";
        Ok(())
    }
    pub fn request(&mut self, mode: Mode, expected: u64, ready: bool) -> Result<()> {
        if !(1..=MAX_GENERATION).contains(&expected) {
            return Err(ModeError(400));
        }
        if expected != self.version {
            return Err(ModeError(409));
        }
        if mode == Mode::Tracking && (!self.connected || !ready) {
            return Err(ModeError(409));
        }
        if mode != self.requested || self.reason.is_some() {
            self.change(
                mode,
                if ready {
                    None
                } else {
                    Some("resources_unavailable")
                },
            )?;
        }
        self.tick()
    }
    /// Internal static fault codes. Only opt-in transient holds retain tracking.
    pub fn protect(&mut self, reason: &'static str) -> Result<()> {
        let transient = matches!(reason, "frames_expired" | "cadence_low");
        if self.requested == Mode::Tracking && self.auto_recover_tracking && transient {
            if !self.tracking_recoverable {
                self.change(Mode::Tracking, Some(reason))?;
                self.tracking_recoverable = true;
                self.tracking_hold_started = Some(self.now());
                self.tracking_hold_events = self.tracking_hold_events.saturating_add(1);
            } else {
                self.frames.clear();
                self.cadence_at = None;
                self.cadence_rate = None;
                self.cadence_count = 0;
                self.state = "degraded";
                self.reason = Some(reason);
            }
            return Ok(());
        }
        if self.requested == Mode::Tracking {
            return self.change(Mode::Eco, Some(reason));
        }
        if transient && self.reason.is_some() && !self.eco_recoverable {
            return Ok(());
        }
        self.eco_recoverable = transient && (self.reason.is_none() || self.eco_recoverable);
        self.state = "degraded";
        self.reason = Some(reason);
        Ok(())
    }
    pub fn acknowledge(
        &mut self,
        boot: u64,
        generation: u64,
        mode: Mode,
        applied: u64,
    ) -> Result<bool> {
        let valid = self.connected
            && Some(boot) == self.boot
            && generation == self.version
            && mode == self.requested
            && applied > 0;
        if !valid || self.ack.is_some_and(|a| a != applied) {
            self.rejected_ack = self.rejected_ack.saturating_add(1);
            return Ok(false);
        }
        if self.ack.is_none() && self.now() - self.requested_at >= 5_000_000 {
            self.protect("ack_timeout")?;
            self.rejected_ack = self.rejected_ack.saturating_add(1);
            return Ok(false);
        }
        if self.ack.is_none() {
            self.ack_at = Some(self.now());
        }
        self.ack = Some(applied);
        Ok(true)
    }
    pub fn allows(&self, frame: &Frame) -> bool {
        self.connected
            && Some(frame.boot) == self.boot
            && frame.generation == Some(self.version)
            && self.ack.is_some_and(|a| frame.captured_us > a)
    }
    /// Caller must ALSO check the clock-based 500ms capture age. Callback must
    /// be bounded, nonblocking RAM-only, and return false if publication fails.
    pub fn publish_result(
        &mut self,
        frame: &Frame,
        publish: impl FnOnce() -> bool,
    ) -> Result<bool> {
        self.publish_if(frame, |_| true, publish)
    }
    /// Additional bounded RAM-only gate, evaluated under the owner's mode lock.
    /// A false gate drops the result without treating it as a publication fault.
    pub fn publish_if(
        &mut self,
        frame: &Frame,
        eligible: impl FnOnce(u64) -> bool,
        publish: impl FnOnce() -> bool,
    ) -> Result<bool> {
        let now = self.now();
        self.tick_at(now)?; // Use the same decision time for watchdog and publication.
        if !self.allows(frame)
            || self
                .frames
                .back()
                .is_some_and(|s| frame.sequence <= s.sequence || frame.captured_us <= s.captured)
        {
            self.rejected_result = self.rejected_result.saturating_add(1);
            return Ok(false);
        }
        let eligible = catch_unwind(AssertUnwindSafe(|| eligible(now))).map_err(|_| {
            let _ = self.protect("publication_failed");
            ModeError(503)
        })?;
        if !eligible {
            self.rejected_result = self.rejected_result.saturating_add(1);
            return Ok(false);
        }
        if !catch_unwind(AssertUnwindSafe(publish)).unwrap_or(false) {
            self.protect("publication_failed")?;
            return Err(ModeError(503));
        }
        if self.frames.len() == 128 {
            self.frames.pop_front();
        }
        self.frames.push_back(Stamp {
            at: now,
            captured: frame.captured_us,
            sequence: frame.sequence,
        });
        self.cadence_at.get_or_insert(now);
        if self.requested == Mode::Tracking {
            self.cadence_count = self.cadence_count.saturating_add(1);
        }
        self.trim(now);
        self.reconcile();
        Ok(true)
    }
    fn trim(&mut self, now: u64) {
        let window = if self.requested == Mode::Tracking {
            10_000_000
        } else {
            18_000_000
        };
        while self.frames.front().is_some_and(|s| now - s.at >= window) {
            self.frames.pop_front();
        }
    }
    fn cadence(&self) -> bool {
        if self.requested == Mode::Tracking {
            return self.cadence_rate.is_some_and(|r| r >= 5.);
        }
        let n = self.frames.len();
        n >= 3
            && self.now() - self.frames[n - 1].at <= 7_000_000
            && (n - 2..n).all(|i| {
                (4_000_000..=6_000_000).contains(&(self.frames[i].at - self.frames[i - 1].at))
            })
    }
    fn reconcile(&mut self) {
        if self.connected
            && (self.eco_recoverable || self.tracking_recoverable)
            && self.recovery_ready
            && self.ack.is_some()
            && self.cadence()
        {
            if self.eco_recoverable {
                self.reason = None;
                self.eco_recoverable = false;
                self.automatic_recoveries = self.automatic_recoveries.saturating_add(1);
            } else if self.requested == Mode::Tracking
                && self
                    .recovery_guard
                    .as_ref()
                    .is_none_or(|check| catch_unwind(AssertUnwindSafe(check)).unwrap_or(false))
                && self
                    .recovery_valid_until
                    .is_none_or(|end| self.now() <= end)
                && self.frames.back().is_some_and(|s| {
                    self.now()
                        .checked_sub(s.at)
                        .is_some_and(|gap| gap <= 500_000)
                })
            {
                self.finish_tracking_hold();
                self.reason = None;
                self.tracking_recoverable = false;
                self.automatic_recoveries = self.automatic_recoveries.saturating_add(1);
                self.tracking_automatic_recoveries =
                    self.tracking_automatic_recoveries.saturating_add(1);
            }
        }
        if self.connected && self.reason.is_none() {
            self.state = if self.ack.is_some() && self.cadence() {
                "active"
            } else {
                "pending"
            };
            if self.requested == Mode::Eco && self.state == "active" {
                self.eco_confirmed = true;
            }
        }
    }
    pub fn tick(&mut self) -> Result<()> {
        self.tick_at(self.now())
    }
    fn tick_at(&mut self, now: u64) -> Result<()> {
        if !self.connected {
            return Ok(());
        }
        self.trim(now);
        if self.ack.is_none() {
            if now - self.requested_at >= 5_000_000 {
                self.protect("ack_timeout")?;
            }
            return Ok(());
        }
        let expired = match self.frames.back() {
            Some(s) => {
                now - s.at
                    > if self.requested == Mode::Tracking {
                        500_000
                    } else {
                        17_000_000
                    }
            }
            None => {
                now - self.ack_at.unwrap()
                    > if self.requested == Mode::Tracking {
                        1_000_000
                    } else {
                        17_000_000
                    }
            }
        };
        if expired {
            return self.protect("frames_expired");
        }
        if self.requested == Mode::Tracking
            && self.cadence_at.is_some_and(|t| now - t >= 10_000_000)
        {
            if now - self.cadence_at.unwrap() >= 20_000_000 || self.cadence_count < 50 {
                return self.protect("cadence_low");
            }
            self.cadence_rate = Some(self.cadence_count as f64 / 10.);
            *self.cadence_at.as_mut().unwrap() += 10_000_000;
            self.cadence_count = 0;
        }
        self.reconcile();
        Ok(())
    }
    pub fn command(&mut self) -> Result<Option<Value>> {
        self.tick()?;
        let now = self.now();
        if !self.connected
            || self.ack.is_some()
            || self.last_command.is_some_and(|t| now - t < 1_000_000)
        {
            return Ok(None);
        }
        self.last_command = Some(now);
        Ok(Some(
            json!({"type":"profile.set", "boot_id":self.boot, "generation":self.version, "mode":self.requested.name()}),
        ))
    }
    pub fn status(&mut self) -> Result<Value> {
        self.tick()?;
        let now = self.now();
        let rate = self
            .frames
            .front()
            .zip(self.frames.back())
            .filter(|(a, b)| b.at > a.at)
            .map_or(0., |(a, b)| {
                (self.frames.len() - 1) as f64 * 1_000_000. / (b.at - a.at) as f64
            });
        let rate = if self.requested == Mode::Tracking {
            self.cadence_rate.unwrap_or(rate)
        } else {
            rate
        };
        let warning = self.requested == Mode::Eco
            && self.ack.is_some()
            && self.reason.is_none()
            && ((self.eco_confirmed && !self.cadence())
                || now - self.frames.back().map_or(self.ack_at.unwrap(), |s| s.at) > 7_000_000);
        let mut result = json!({"schema":2,"version":self.version,"boot_id":self.boot,"requested_mode":self.requested.name(),
            "effective_mode":if self.ack.is_some() && !self.frames.is_empty() { Some(self.requested.name()) } else { None },
            "state":self.state,"reason":self.reason,"connected":self.connected,"eco_warning":warning,
            "eco_auto_recovery_pending":self.eco_recoverable,"automatic_recoveries":self.automatic_recoveries,
            "actuation_state":if self.state == "active" && self.requested == Mode::Tracking { "observe_only" } else { "hold" },
            "acknowledged":self.ack.is_some(),"last_result_age_seconds":self.frames.back().map(|s| (now-s.at) as f64 / 1_000_000.),
            "recognition_hz":rate,"cadence_samples":self.frames.len(),"transitions":self.transitions,
            "rejected_ack":self.rejected_ack,"rejected_result":self.rejected_result});
        if self.auto_recover_tracking {
            let elapsed = self
                .tracking_hold_started
                .map_or(0, |start| now.saturating_sub(start));
            let fields = result.as_object_mut().unwrap();
            fields.insert(
                "tracking_auto_recovery_pending".into(),
                json!(self.tracking_recoverable),
            );
            fields.insert(
                "tracking_hold_events".into(),
                json!(self.tracking_hold_events),
            );
            fields.insert(
                "tracking_automatic_recoveries".into(),
                json!(self.tracking_automatic_recoveries),
            );
            fields.insert(
                "tracking_hold_seconds".into(),
                json!(self.tracking_hold_total.saturating_add(elapsed) as f64 / 1_000_000.),
            );
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests;
