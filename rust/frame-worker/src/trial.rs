//! Finite observe-only trial policy. This never issues physical motor commands.
use crate::{
    operating_mode::Mode,
    pipeline::{MAX_SAMPLES, Sample, State},
};
use serde_json::{Value, json};
pub const MAX_DURATION_US: u64 = 3_600_000_000;

type Result<T> = std::result::Result<T, &'static str>;
/// Explicit experiment duration; the existing sixty-second default is unchanged.
pub fn duration_us(value: Option<&str>) -> Result<u64> {
    let raw = value.unwrap_or("60");
    if raw.is_empty()
        || raw.len() > 4
        || raw.starts_with('0')
        || !raw.bytes().all(|c| c.is_ascii_digit())
    {
        return Err("invalid_duration");
    }
    let micros = raw.parse::<u64>().map_err(|_| "invalid_duration")? * 1_000_000;
    if !(10_000_000..=MAX_DURATION_US).contains(&micros) || !micros.is_multiple_of(10_000_000) {
        return Err("invalid_duration");
    }
    Ok(micros)
}

#[derive(PartialEq, Eq)]
enum Phase {
    InitialEco,
    Warmup,
    Tracking,
    FinalEco,
    Done,
}
pub struct Trial {
    phase: Phase,
    boot: u64,
    duration: u64,
    changed: Option<u64>,
    generation: Option<u64>,
    start: Option<u64>,
    end: Option<u64>,
    pub initial: Value,
    pub tracking: Value,
    pub final_eco: Value,
    continuous: bool,
    epochs: Vec<(u64, u64)>,
    holding_since: Option<u64>,
    inject_hold: bool,
    injected: bool,
    injected_at: Option<(u64, u64)>,
    holds: Vec<(u64, u64, u64)>,
}
impl Trial {
    pub fn new(boot: u64, duration_us: u64) -> Result<Self> {
        if boot == 0
            || !(10_000_000..=MAX_DURATION_US).contains(&duration_us)
            || !duration_us.is_multiple_of(10_000_000)
        {
            return Err("invalid_trial");
        }
        Ok(Self {
            phase: Phase::InitialEco,
            boot,
            duration: duration_us,
            changed: None,
            generation: None,
            start: None,
            end: None,
            initial: Value::Null,
            tracking: Value::Null,
            final_eco: Value::Null,
            continuous: false,
            epochs: Vec::new(),
            holding_since: None,
            inject_hold: false,
            injected: false,
            injected_at: None,
            holds: Vec::new(),
        })
    }
    pub fn new_continuous(boot: u64, duration_us: u64, inject_hold: bool) -> Result<Self> {
        let mut trial = Self::new(boot, duration_us)?;
        if inject_hold && duration_us < 60_000_000 {
            return Err("invalid_injection_duration");
        }
        trial.continuous = true;
        trial.inject_hold = inject_hold;
        Ok(trial)
    }
    fn recovery_epoch(&mut self, now: u64, generation: u64, holds: Option<u64>) -> Result<()> {
        if self.generation.and_then(|g| g.checked_add(1)) != Some(generation)
            || self.epochs.len() >= 129
            || holds != Some(self.epochs.len() as u64)
        {
            return Err("invalid_recovery_epoch");
        }
        self.generation = Some(generation);
        self.epochs.push((now, generation));
        Ok(())
    }
    /// Called with the State mode lock held; readiness is a cached supervisor signal.
    pub fn poll(&mut self, state: &mut State, now: u64, ready: bool) -> Result<bool> {
        if !ready {
            let _ = state.mode.protect("resources_unavailable");
            return Err("resources_unavailable");
        }
        if state.counters.failures > 0 || state.counters.evidence_lost > 0 {
            return Err("inference_or_evidence_failed");
        }
        if self.phase == Phase::Done {
            return Ok(false);
        }
        let changed = *self.changed.get_or_insert(now);
        let status = state.mode.status().map_err(|_| "mode_failed")?;
        if self.continuous && !status["tracking_hold_events"].is_u64() {
            return Err("recovery_policy_unavailable");
        }
        if status["boot_id"].as_u64().is_some_and(|b| b != self.boot) {
            return Err("camera_rebooted");
        }
        let connected = status["connected"] == true;
        let recovering = self.continuous
            && status["tracking_auto_recovery_pending"] == true
            && state.mode.requested() == Mode::Tracking
            && matches!(
                status["reason"].as_str(),
                Some("frames_expired" | "cadence_low")
            );
        // status() can create or finish a hold after the caller's earlier stamp.
        let proof_now = if self.continuous { state.now() } else { now };
        if recovering {
            let began = state
                .mode
                .tracking_hold_started()
                .ok_or("missing_hold_start")?;
            self.holding_since.get_or_insert(began);
            if self.holding_since != Some(began) {
                return Err("hold_start_changed");
            }
            if proof_now
                .checked_sub(began)
                .is_none_or(|age| age > 30_000_000)
            {
                return Err("tracking_recovery_timeout");
            }
        } else if let Some(began) = self.holding_since.take()
            && status["reason"].is_null()
            && state.mode.requested() == Mode::Tracking
        {
            let (start, end) = state.mode.last_tracking_hold().ok_or("missing_hold_end")?;
            if start != began
                || end < start
                || end > proof_now
                || end - start > 30_000_000
                || self.holds.len() >= 128
                || status["tracking_automatic_recoveries"].as_u64()
                    != Some(self.holds.len() as u64 + 1)
            {
                return Err("invalid_hold_period");
            }
            self.holds.push((state.mode.generation(), start, end));
        }
        if (self.phase != Phase::InitialEco || connected)
            && !status["reason"].is_null()
            && !recovering
        {
            return Err("mode_degraded");
        }
        let active = status["state"] == "active" && status["acknowledged"] == true;
        match self.phase {
            Phase::InitialEco => {
                if active && state.mode.requested() == Mode::Eco {
                    self.initial = status;
                    state
                        .mode
                        .request(Mode::Tracking, state.mode.generation(), true)
                        .map_err(|_| "tracking_unavailable")?;
                    self.generation = Some(state.mode.generation());
                    self.epochs.push((now, state.mode.generation()));
                    self.phase = Phase::Warmup;
                    self.changed = Some(now);
                } else if now.checked_sub(changed).is_none_or(|t| t > 40_000_000) {
                    return Err("initial_eco_timeout");
                }
            }
            Phase::Warmup | Phase::Tracking => {
                if Some(state.mode.generation()) != self.generation && recovering {
                    self.recovery_epoch(
                        proof_now,
                        state.mode.generation(),
                        status["tracking_hold_events"].as_u64(),
                    )?;
                }
                if !connected
                    || state.mode.requested() != Mode::Tracking
                    || Some(state.mode.generation()) != self.generation
                {
                    return Err("tracking_changed");
                }
                if self.phase == Phase::Warmup {
                    if active {
                        self.phase = Phase::Tracking;
                        self.changed = Some(now);
                    } else if !recovering && now.checked_sub(changed).is_none_or(|t| t > 20_000_000)
                    {
                        return Err("tracking_timeout");
                    }
                } else {
                    if self.start.is_none() {
                        let anchor = state.samples().last().filter(|s| {
                            s.boot == self.boot
                                && Some(s.generation) == self.generation
                                && changed <= s.published_us
                                && s.published_us <= now
                        });
                        if let Some(sample) = anchor {
                            self.start = Some(sample.published_us);
                        } else if now.checked_sub(changed).is_none_or(|age| age > 500_000) {
                            return Err("missing_tracking_sample");
                        } else {
                            return Ok(true);
                        }
                    }
                    if self.continuous
                        && self.inject_hold
                        && !self.injected
                        && active
                        && now - self.start.unwrap() >= 30_000_000
                    {
                        self.injected = true;
                        state
                            .mode
                            .protect("frames_expired")
                            .map_err(|_| "mode_failed")?;
                        self.injected_at = Some((
                            state
                                .mode
                                .tracking_hold_started()
                                .ok_or("missing_hold_start")?,
                            state.mode.generation(),
                        ));
                        return Ok(true);
                    }
                    if now
                        .checked_sub(self.start.unwrap())
                        .ok_or("invalid_clock")?
                        >= self.duration
                    {
                        if !active {
                            if recovering {
                                return Ok(true);
                            }
                            return Err("tracking_inactive");
                        }
                        self.end = Some(
                            self.start
                                .unwrap()
                                .checked_add(self.duration)
                                .ok_or("invalid_clock")?,
                        );
                        self.tracking = status;
                        state
                            .mode
                            .request(Mode::Eco, state.mode.generation(), true)
                            .map_err(|_| "eco_unavailable")?;
                        self.phase = Phase::FinalEco;
                        self.changed = Some(now);
                    }
                }
            }
            Phase::FinalEco => {
                if active && connected && state.mode.requested() == Mode::Eco {
                    self.final_eco = status;
                    self.phase = Phase::Done;
                    return Ok(false);
                }
                if now.checked_sub(changed).is_none_or(|t| t > 40_000_000) {
                    return Err("final_eco_timeout");
                }
            }
            Phase::Done => {}
        }
        Ok(true)
    }
    pub fn evaluate(&self, samples: &[Sample]) -> Result<Value> {
        if self.phase != Phase::Done {
            return Err("trial_incomplete");
        }
        let (start, end, generation) = (
            self.start.unwrap(),
            self.end.unwrap(),
            self.generation.unwrap(),
        );
        let records: Vec<_> = samples
            .iter()
            .filter(|s| start <= s.published_us && s.published_us < end)
            .collect();
        if records.is_empty() || records.len() > MAX_SAMPLES {
            return Err("invalid_samples");
        }
        let mut counts = vec![0u64; (self.duration / 10_000_000) as usize];
        let mut ages = Vec::new();
        let mut compute = Vec::new();
        let (mut previous, mut sequence, mut gap) = (start, 0, 0);
        for s in &records {
            let expected = if self.continuous {
                self.epochs
                    .iter()
                    .rev()
                    .find(|(at, _)| *at <= s.published_us)
                    .map(|(_, g)| *g)
            } else {
                Some(generation)
            };
            if s.boot != self.boot
                || Some(s.generation) != expected
                || s.sequence <= sequence
                || s.published_us < previous
                || !(0..=500_000).contains(&s.age_high_us)
            {
                return Err("invalid_samples");
            }
            gap = gap.max(s.published_us - previous);
            previous = s.published_us;
            sequence = s.sequence;
            counts[((s.published_us - start) / 10_000_000) as usize] += 1;
            ages.push(s.age_high_us);
            compute.push(s.compute_us);
        }
        gap = gap.max(end - previous);
        let strict_rate = gap <= 500_000 && counts.iter().all(|n| *n >= 50);
        if (!self.continuous && !strict_rate)
            || (self.continuous
                && (records.len() as u64 * 1_000_000 < self.duration * 5
                    || self.tracking["tracking_hold_events"]
                        != self.tracking["tracking_automatic_recoveries"]
                    || self.tracking["tracking_hold_events"].as_u64()
                        != Some(self.holds.len() as u64)
                    || (self.inject_hold && !self.injected)))
        {
            return Err("tracking_below_target");
        }
        ages.sort_unstable();
        if self.continuous && self.inject_hold {
            let (at, injected_generation) = self.injected_at.ok_or("missing_injection_evidence")?;
            if !(start..end).contains(&at) {
                return Err("missing_in_window_recovery");
            }
            let recovered = self
                .holds
                .iter()
                .find(|(g, began, _)| *g == injected_generation && *began == at)
                .map(|(_, _, end)| *end)
                .ok_or("missing_injection_evidence")?;
            let verified = recovered < end
                && records
                    .iter()
                    .any(|s| s.generation == injected_generation && s.published_us >= recovered)
                && records
                    .iter()
                    .any(|s| s.generation != injected_generation && s.published_us < at);
            if !verified {
                return Err("missing_in_window_recovery");
            }
        }
        compute.sort_unstable();
        let p95 = (records.len() * 95).div_ceil(100) - 1;
        let mut report = json!({"duration_us":self.duration,"results":records.len(),"recognition_hz":records.len() as f64*1_000_000./self.duration as f64,
            "window_results":counts,"max_gap_us":gap,"age_p95_us":ages[p95],"age_max_us":ages.last(),"compute_p95_us":compute[p95],
            "boot_id":self.boot,"generation":generation,"start_us":start,"end_us":end});
        if self.continuous {
            let fields = report.as_object_mut().unwrap();
            fields.insert("policy".into(), json!("continuous-recovery-v1"));
            fields.insert("strict_rate_gate_passed".into(), json!(strict_rate));
            fields.insert(
                "strict_single_generation_gate_passed".into(),
                json!(
                    records
                        .iter()
                        .all(|s| s.generation == records[0].generation)
                ),
            );
            fields.insert("generation_epochs".into(), json!(self.epochs));
            fields.insert("injected_hold".into(), json!(self.injected));
            fields.insert("injected_hold_at".into(), json!(self.injected_at));
            fields.insert("tracking_holds".into(), json!(self.holds));
            fields.insert(
                "tracking_hold_statistics_scope".into(),
                json!("full_tracking_phase"),
            );
            fields.insert(
                "tracking_hold_seconds".into(),
                self.tracking["tracking_hold_seconds"].clone(),
            );
            fields.insert(
                "tracking_hold_events".into(),
                self.tracking["tracking_hold_events"].clone(),
            );
            fields.insert(
                "tracking_automatic_recoveries".into(),
                self.tracking["tracking_automatic_recoveries"].clone(),
            );
        }
        Ok(report)
    }
}

/// Supervisor wall-clock ticket, independent of the monotonic camera age proof.
/// Old queued tickets and clock jumps fail closed; raw input is never logged.
pub fn heartbeat(raw: &[u8], nonce: &str, unix_ms: u64) -> bool {
    if raw.len() > 128 || nonce.len() != 16 || !nonce.bytes().all(|b| b.is_ascii_hexdigit()) {
        return false;
    }
    let Ok(line) = std::str::from_utf8(raw) else {
        return false;
    };
    let fields: Vec<_> = line.split_ascii_whitespace().collect();
    if fields.len() != 3 || fields[0] != "ready" || fields[1] != nonce {
        return false;
    }
    fields[2]
        .parse::<u64>()
        .is_ok_and(|sent| unix_ms.abs_diff(sent) <= 1500)
}

#[cfg(test)]
mod tests;
