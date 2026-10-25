//! One-camera finite session coordinator; no native inference or disk I/O here.
use crate::{
    FrameOrder,
    control::{self, Control, SessionClock},
    decode,
    operating_mode::Mode,
    pipeline::{Clock, Job, State, Submit, Worker},
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
pub struct Receipt {
    pub sequence: u64,
    pub received_us: u64,
    pub ws_complete_unix_us: Option<u64>,
}
#[derive(Default)]
pub struct Stats {
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
    pub first_device: Option<[u64; 6]>,
    pub last_device: Option<[u64; 6]>,
}
impl Stats {
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
}
impl<'a> Receiver<'a> {
    pub fn new(worker: &'a Worker, limits: Limits) -> Result<Self> {
        if !(1..=600_000_000).contains(&limits.duration_us)
            || !(1..=20_000).contains(&limits.messages)
            || !(1..=7000).contains(&limits.frames)
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
        })
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
            .and_then(|mut s| s.claim_session())
        {
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
        let final_status = self.snapshot_and_cancel(&token);
        Outcome {
            reason: result.err().unwrap_or("completed"),
            stats,
            final_status,
        }
    }
    fn snapshot_and_cancel(&self, token: &AtomicBool) -> Value {
        match self.state.lock() {
            Ok(mut s) => {
                token.store(true, Ordering::Release);
                s.mode.status().unwrap_or(Value::Null)
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
                match catch_unwind(AssertUnwindSafe(|| controller(&mut s, now))) {
                    Ok(Ok(true)) => {}
                    Ok(result) => {
                        token.store(true, Ordering::Release);
                        return result.map(|_| ());
                    }
                    Err(_) => {
                        token.store(true, Ordering::Release);
                        let _ = s.mode.protect("controller_failed");
                        return Err("controller_failed");
                    }
                }
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
                        } => clock.pong(boot, &nonce, device_us, received)?,
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
                        stats.dropped += 1;
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
                        stats.dropped += 1;
                        continue;
                    }
                    let Some(arrival) = clock.admit(&frame, received) else {
                        stats.dropped += 1;
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
