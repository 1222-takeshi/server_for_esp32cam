//! One-camera finite session coordinator; no native inference or disk I/O here.
use crate::{
    FrameOrder,
    control::{self, Control, SessionClock},
    decode,
    operating_mode::Mode,
    pipeline::{Clock, Job, State, Submit, TimingSnapshot, Worker},
    transport::{self, Connection, Message},
};
use serde_json::Value;
use std::collections::VecDeque;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};

type Result<T> = std::result::Result<T, &'static str>;
#[derive(Clone, Copy)]
pub struct Limits {
    pub duration_us: u64,
    pub messages: u64,
    pub frames: u64,
    pub rate: u64,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            duration_us: 600_000_000,
            messages: 20_000,
            frames: 7000,
            rate: 100,
        }
    }
}
#[derive(Clone, Copy)]
pub struct Receipt {
    pub sequence: u64,
    pub received_us: u64,
    pub ws_complete_unix_us: Option<u64>,
}
pub struct Rejection {
    pub boot: u64,
    pub generation: Option<u64>,
    pub sequence: u64,
    pub captured_us: u64,
    pub sent_us: u64,
    pub received_us: u64,
    pub stage: &'static str,
    pub age_bounds: Option<crate::clock::AgeBounds>,
    pub clock_exchanges: Vec<[u64; 4]>,
}
struct RejectionTiming {
    sequence: u64,
    generation: Option<u64>,
    received_us: u64,
    captured_us: u64,
    sent_us: u64,
    stage: &'static str,
    age_bounds: Option<crate::clock::AgeBounds>,
}
struct HoldDiagnostic {
    generation: u64,
    started_us: u64,
    reason: Option<&'static str>,
    pipeline: TimingSnapshot,
    after: Option<TimingSnapshot>,
    watched_sequence: Option<u64>,
    after_source: Option<&'static str>,
    receipts: [Option<Receipt>; 8],
    io: [Option<transport::IoSample>; 8],
    rejection: Option<RejectionTiming>,
    counts: [u64; 5],
}
impl HoldDiagnostic {
    fn report(&self) -> Value {
        let receipts:Vec<_>=self.receipts.iter().flatten().map(|r|serde_json::json!({
            "sequence":r.sequence,"received_us":r.received_us,"ws_complete_unix_us":r.ws_complete_unix_us})).collect();
        let io: Vec<_> = self
            .io
            .iter()
            .flatten()
            .map(|e| {
                serde_json::json!({
            "kind":e.kind,"status":e.status,"bytes":e.bytes,"elapsed_us":e.elapsed_us,
            "observed_unix_us":e.observed_unix_us})
            })
            .collect();
        let rejection=self.rejection.as_ref().map(|r|serde_json::json!({
            "sequence":r.sequence,"generation":r.generation,"received_us":r.received_us,
            "captured_us":r.captured_us,"sent_us":r.sent_us,"stage":r.stage,"age_bounds_us":r.age_bounds}));
        serde_json::json!({"generation":self.generation,"started_us":self.started_us,"reason":self.reason,
            "pipeline":self.pipeline.report(),"after":self.after.as_ref().map(TimingSnapshot::report),
            "watched_sequence":self.watched_sequence,"after_source":self.after_source,
            "receipts":receipts,"io":io,"last_rejection":rejection,
            "frames":self.counts[0],"admitted":self.counts[1],"dropped":self.counts[2],
            "overwritten":self.counts[3],"queue_busy":self.counts[4]})
    }
}
pub struct HoldDiagnostics(Vec<HoldDiagnostic>);
impl Default for HoldDiagnostics {
    fn default() -> Self {
        Self(Vec::with_capacity(128))
    }
}
impl HoldDiagnostics {
    /// Serialize only after publication has stopped; never under the live State lock.
    pub fn report(&self) -> Vec<Value> {
        self.0.iter().map(HoldDiagnostic::report).collect()
    }
}
#[derive(Default)]
pub struct Stats {
    pub hold_diagnostics: HoldDiagnostics,
    pub hold_diagnostics_lost: u64,
    initial_hold_events: u64,
    last_hold: Option<(u64, u64)>,
    last_hold_saved: bool,
    pub clock_restarts: u64,
    pub retired_pongs: u64,
    pub recent_clock_restarts: VecDeque<[u64; 2]>,
    pub messages: u64,
    pub frames: u64,
    pub admitted: u64,
    pub dropped: u64,
    pub overwritten: u64,
    pub queue_busy: u64,
    pub rejected_ack: u64,
    pub max_receive_call_us: u64,
    pub max_control_send_us: u64,
    pub max_admission_guard_us: u64,
    pub recent_receipts: VecDeque<Receipt>,
    pub recent_rejections: VecDeque<Rejection>,
    pub first_device: Option<[u64; 6]>,
    pub last_device: Option<[u64; 6]>,
}
impl Stats {
    fn note_hold(&mut self, state: &State, io: &VecDeque<transport::IoSample>) {
        let Some(started) = state.mode.tracking_hold_started() else {
            return;
        };
        let key = (state.mode.generation(), started);
        if self.last_hold == Some(key) {
            if self.last_hold_saved {
                let last = self.hold_diagnostics.0.last_mut().unwrap();
                if last.after.is_none()
                    && last
                        .watched_sequence
                        .is_none_or(|seq| state.in_flight_sequence() != Some(seq))
                {
                    last.after = Some(state.timing_snapshot(state.now()));
                    last.after_source = Some("poll");
                }
            }
            return;
        }
        self.last_hold = Some(key);
        self.last_hold_saved = false;
        if self.hold_diagnostics.0.len() == 128 {
            self.hold_diagnostics_lost = self.hold_diagnostics_lost.saturating_add(1);
            return;
        }
        self.last_hold_saved = true;
        let receipts = std::array::from_fn(|i| {
            self.recent_receipts
                .get(self.recent_receipts.len().saturating_sub(8) + i)
                .copied()
        });
        let io = std::array::from_fn(|i| io.get(io.len().saturating_sub(8) + i).copied());
        let rejection = self.recent_rejections.back().map(|r| RejectionTiming {
            sequence: r.sequence,
            generation: r.generation,
            received_us: r.received_us,
            captured_us: r.captured_us,
            sent_us: r.sent_us,
            stage: r.stage,
            age_bounds: r.age_bounds,
        });
        self.hold_diagnostics.0.push(HoldDiagnostic {
            generation: key.0,
            started_us: started,
            reason: state.mode.tracking_hold_reason(),
            pipeline: state.timing_snapshot(state.now()),
            after: None,
            watched_sequence: state.in_flight_sequence(),
            after_source: None,
            receipts,
            io,
            rejection,
            counts: [
                self.frames,
                self.admitted,
                self.dropped,
                self.overwritten,
                self.queue_busy,
            ],
        });
    }
    pub fn finish_diagnostics(&mut self, state: &State) {
        if let Some(last) = self.hold_diagnostics.0.last_mut()
            && last.after.is_none()
        {
            last.after = Some(state.timing_snapshot(state.now()));
            last.after_source = Some("worker_shutdown");
        }
        self.hold_diagnostics_lost = self.hold_diagnostics_lost.max(
            state
                .mode
                .tracking_hold_events()
                .saturating_sub(self.initial_hold_events)
                .saturating_sub(self.hold_diagnostics.0.len() as u64),
        );
    }
    fn reject(
        &mut self,
        frame: &crate::Frame,
        received_us: u64,
        stage: &'static str,
        clock: &SessionClock,
    ) {
        self.dropped += 1;
        let (age_bounds, clock_exchanges) = clock.diagnostic(frame, received_us);
        let stage = if stage == "clock" {
            if age_bounds.is_some() {
                "age_limit"
            } else {
                "clock_unavailable"
            }
        } else {
            stage
        };
        if self.recent_rejections.len() == 32 {
            self.recent_rejections.pop_front();
        }
        self.recent_rejections.push_back(Rejection {
            boot: frame.boot,
            generation: frame.generation,
            sequence: frame.sequence,
            captured_us: frame.captured_us,
            sent_us: frame.sent_us,
            received_us,
            stage,
            age_bounds,
            clock_exchanges,
        });
    }

    fn receipt(&mut self, sequence: u64, received_us: u64, ws_complete_unix_us: Option<u64>) {
        if self.recent_receipts.len() == 32 {
            self.recent_receipts.pop_front();
        }
        self.recent_receipts.push_back(Receipt {
            sequence,
            received_us,
            ws_complete_unix_us,
        });
    }
}
pub struct Outcome {
    pub reason: &'static str,
    pub stats: Stats,
    pub final_status: Value,
}
struct Lease {
    state: Arc<Mutex<State>>,
    token: Arc<AtomicBool>,
}
impl Drop for Lease {
    fn drop(&mut self) {
        match self.state.lock() {
            Ok(mut s) => s.release_session(&self.token),
            Err(_) => self.token.store(true, Ordering::Release),
        }
    }
}
struct SocketShutdown<'a>(&'a mut Connection);
impl Drop for SocketShutdown<'_> {
    fn drop(&mut self) {
        self.0.shutdown();
    }
}
pub struct Receiver<'a> {
    state: Arc<Mutex<State>>,
    clock: Clock,
    worker: &'a Worker,
    limits: Limits,
    recover_clock: bool,
}
impl<'a> Receiver<'a> {
    pub fn new(worker: &'a Worker, limits: Limits) -> Result<Self> {
        if !(1..=3_720_000_000).contains(&limits.duration_us)
            || !(1..=200_000).contains(&limits.messages)
            || !(1..=50_000).contains(&limits.frames)
            || !(1..=100).contains(&limits.rate)
        {
            return Err("invalid_limits");
        }
        let state = worker.state();
        let clock = worker.clock();
        Ok(Self {
            state,
            clock,
            worker,
            limits,
            recover_clock: false,
        })
    }
    /// Explicit continuous tracking policy; strict sessions retain their default.
    pub fn with_clock_recovery(mut self) -> Self {
        self.recover_clock = true;
        self
    }
    fn restart_clock(
        &self,
        s: &mut State,
        clock: &mut SessionClock,
        now: u64,
        stats: &mut Stats,
    ) -> Result<()> {
        if !self.recover_clock || clock.expired_at(now)?.is_none() {
            return Ok(());
        }
        if s.mode.requested() != Mode::Tracking {
            return Ok(());
        }
        if stats.clock_restarts >= 128 {
            s.mode
                .protect("clock_recovery_limit")
                .map_err(|_| "mode_failed")?;
            return Err("clock_recovery_limit");
        }
        let fenced = s.invalidate_clock().map_err(|_| "mode_failed")?;
        let sent = clock.restart_expired(fenced)?;
        stats.clock_restarts += 1;
        if stats.recent_clock_restarts.len() == 8 {
            stats.recent_clock_restarts.pop_front();
        }
        stats.recent_clock_restarts.push_back([sent, fenced]);
        Ok(())
    }
    fn receive(
        &self,
        socket: &mut Connection,
        stats: &mut Stats,
    ) -> Result<Option<(Message, u64)>> {
        let begun = (self.clock)();
        let result = socket.receive().map_err(|_| "socket_failed");
        // Capture immediately after receive, before acquiring the publication lock.
        let received = (self.clock)();
        stats.max_receive_call_us = stats
            .max_receive_call_us
            .max(received.saturating_sub(begun));
        result.map(|message| message.map(|m| (m, received)))
    }
    fn send(&self, socket: &mut Connection, message: &str, stats: &mut Stats) -> Result<()> {
        let begun = (self.clock)();
        let result = socket.send_text(message).map_err(|_| "socket_failed");
        stats.max_control_send_us = stats
            .max_control_send_us
            .max((self.clock)().saturating_sub(begun));
        result
    }
    /// Controller runs under the mode mutex: bounded, RAM-only, no blocking IO.
    /// false ends normally. The socket closes before releasing the session lease.
    pub fn run(
        &self,
        socket: &mut Connection,
        mut controller: impl FnMut(&mut State, u64) -> Result<bool>,
    ) -> Outcome {
        let mut stats = Stats::default();
        let token = match self
            .state
            .lock()
            .map_err(|_| "state_unavailable")
            .and_then(|mut s| {
                let token = s.claim_session()?;
                stats.initial_hold_events = s.mode.tracking_hold_events();
                Ok(token)
            }) {
            Ok(token) => token,
            Err(reason) => {
                socket.shutdown();
                return Outcome {
                    reason,
                    stats,
                    final_status: Value::Null,
                };
            }
        };
        let _lease = Lease {
            state: self.state.clone(),
            token: token.clone(),
        };
        let shutdown = SocketShutdown(socket); // Drops before the earlier lease, including unwinding.
        let result = self.exchange(shutdown.0, &token, &mut stats, &mut controller);
        let final_status = self.snapshot_and_cancel(&token, &mut stats, shutdown.0.io_trace());
        Outcome {
            reason: result.err().unwrap_or("completed"),
            stats,
            final_status,
        }
    }
    fn snapshot_and_cancel(
        &self,
        token: &AtomicBool,
        stats: &mut Stats,
        io: &VecDeque<transport::IoSample>,
    ) -> Value {
        match self.state.lock() {
            Ok(mut s) => {
                token.store(true, Ordering::Release);
                let status = s.mode.status().unwrap_or(Value::Null);
                stats.note_hold(&s, io);
                status
            }
            Err(_) => {
                token.store(true, Ordering::Release);
                Value::Null
            }
        }
    }
    fn exchange(
        &self,
        socket: &mut Connection,
        token: &Arc<AtomicBool>,
        stats: &mut Stats,
        controller: &mut impl FnMut(&mut State, u64) -> Result<bool>,
    ) -> Result<()> {
        let begun = self.state.lock().map_err(|_| "state_unavailable")?.now();
        let mut clock = SessionClock::default();
        let mut order = FrameOrder::default();
        let (mut window, mut rate) = (begun, 0);
        loop {
            let (now, tracking, command) = {
                let mut s = self.state.lock().map_err(|_| "state_unavailable")?;
                let now = s.now();
                if now
                    .checked_sub(begun)
                    .is_none_or(|age| age >= self.limits.duration_us)
                {
                    return Err("duration_limit");
                }
                // Same lock and fresh decision stamp as the controller's commit.
                self.restart_clock(&mut s, &mut clock, now, stats)?;
                stats.note_hold(&s, socket.io_trace());
                match catch_unwind(AssertUnwindSafe(|| controller(&mut s, now))) {
                    Ok(Ok(true)) => {}
                    Ok(result) => {
                        stats.note_hold(&s, socket.io_trace());
                        token.store(true, Ordering::Release);
                        return result.map(|_| ());
                    }
                    Err(_) => {
                        token.store(true, Ordering::Release);
                        let _ = s.mode.protect("controller_failed");
                        return Err("controller_failed");
                    }
                }
                stats.note_hold(&s, socket.io_trace());
                let command = s.mode.command().map_err(|_| "mode_failed")?;
                if !s.mode.connected() && now - begun >= 5_000_000 {
                    return Err("hello_timeout");
                }
                (now, s.mode.requested() == Mode::Tracking, command)
            };
            if let Some(command) = command {
                self.send(socket, &command.to_string(), stats)?;
            }
            if let Some(ping) = clock.poll(now, tracking, || {
                transport::nonce().map_err(|_| "entropy_unavailable")
            })? {
                self.send(socket, &ping.to_string(), stats)?;
            }
            let Some((message, received)) = self.receive(socket, stats)? else {
                continue;
            };
            // Receive can cross the deadline: fence before accepting any payload.
            if self.recover_clock {
                let mut s = self.state.lock().map_err(|_| "state_unavailable")?;
                let now = s.now();
                self.restart_clock(&mut s, &mut clock, now, stats)?;
            }
            if received.checked_sub(window).ok_or("invalid_host_clock")? >= 1_000_000 {
                window = received;
                rate = 0;
            }
            rate += 1;
            stats.messages += 1;
            if rate > self.limits.rate {
                return Err("message_rate");
            }
            if stats.messages > self.limits.messages {
                return Err("message_limit");
            }
            match message {
                Message::Text(raw) => {
                    let item = control::parse(&raw)?;
                    clock.bind(item.boot())?;
                    match item {
                        Control::Hello { boot, floor } => self
                            .state
                            .lock()
                            .map_err(|_| "state_unavailable")?
                            .mode
                            .hello(boot, floor)
                            .map_err(|_| "invalid_hello")?,
                        Control::Ack {
                            boot,
                            generation,
                            mode,
                            applied_us,
                        } => {
                            if !self
                                .state
                                .lock()
                                .map_err(|_| "state_unavailable")?
                                .mode
                                .acknowledge(boot, generation, mode, applied_us)
                                .map_err(|_| "mode_failed")?
                            {
                                stats.rejected_ack += 1;
                            }
                        }
                        Control::Pong {
                            boot,
                            nonce,
                            device_us,
                        } => {
                            if self.recover_clock && clock.ignore_retired(boot, &nonce)? {
                                stats.retired_pongs += 1;
                            } else {
                                clock.pong(boot, &nonce, device_us, received)?;
                            }
                        }
                        Control::Status { counters, .. } => {
                            stats.first_device.get_or_insert(counters);
                            stats.last_device = Some(counters);
                        }
                    }
                }
                Message::Binary(raw) => {
                    stats.frames += 1;
                    if stats.frames > self.limits.frames {
                        return Err("frame_limit");
                    }
                    let frame = decode(&raw, true).map_err(|_| "invalid_frame")?;
                    if frame.generation.is_none() {
                        return Err("profile_required");
                    }
                    clock.bind(frame.boot)?;
                    let wall = socket
                        .io_trace()
                        .back()
                        .filter(|e| e.kind == "binary" && e.status == "complete")
                        .and_then(|e| e.observed_unix_us);
                    stats.receipt(frame.sequence, received, wall);
                    if order.observe(&frame).is_err() {
                        stats.reject(&frame, received, "order", &clock);
                        continue;
                    }
                    let begun_guard = (self.clock)();
                    let allowed = self
                        .state
                        .lock()
                        .map_err(|_| "state_unavailable")?
                        .mode
                        .allows(&frame);
                    stats.max_admission_guard_us = stats
                        .max_admission_guard_us
                        .max((self.clock)().saturating_sub(begun_guard));
                    if !allowed {
                        stats.reject(&frame, received, "mode", &clock);
                        continue;
                    }
                    let Some(arrival) = clock.admit(&frame, received) else {
                        stats.reject(&frame, received, "clock", &clock);
                        continue;
                    };
                    match self.worker.submit(Job {
                        frame,
                        arrival,
                        cancelled: token.clone(),
                    }) {
                        Submit::Queued => stats.admitted += 1,
                        Submit::Overwritten => {
                            stats.admitted += 1;
                            stats.overwritten += 1;
                            stats.dropped += 1;
                        }
                        Submit::Busy => {
                            stats.queue_busy += 1;
                            stats.dropped += 1;
                        }
                        Submit::Stopped => return Err("worker_stopped"),
                        Submit::Invalid => return Err("invalid_job"),
                    }
                }
                Message::Ping(_) | Message::Pong(_) => {}
                _ => return Err("invalid_message"),
            }
        }
    }
}

#[cfg(test)]
mod tests;
