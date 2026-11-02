//! Online evaluation of the live abort conditions. Pure logic over values the
//! caller already owns; every time is native monotonic microseconds.
//!
//! The caller invokes [`OnlineGuard::observe`] on every controller pass, whether
//! or not a result was published, so a stalled publication path cannot hide a
//! violation. Hold and protection checks run on every call, with every age
//! evaluated at the current time; the window scan runs on a one second
//! schedule. A result about to be published is checked against the same limits
//! by the publisher, which refuses it and reports the refusal here.
use crate::pipeline::Sample;

pub const CONTINUOUS_HOLD_US: u64 = 15_000_000;
pub const WINDOW_US: u64 = 10_000_000;
pub const MIN_RECOGNITIONS: usize = 50;
pub const AGE_LIMIT_US: i128 = 500_000;
pub const RTT_LIMIT_US: u64 = 1_000_000;
pub const SYNC_LIMIT_US: u64 = 10_000_000;
pub const TICK_US: u64 = 1_000_000;
pub const EXIT_CODE: i32 = 20;

pub const CONTINUOUS_HOLD: &str = "continuous_hold";
pub const NATURAL_HOLD_BUDGET: &str = "natural_hold_budget_exceeded";
pub const WINDOW_BELOW_50: &str = "window_below_50";
pub const PROTECTION_VIOLATION: &str = "protection_violation";

/// Whether `reason` is one of the codes this module can produce.
pub fn is_abort_reason(reason: &str) -> bool {
    [
        CONTINUOUS_HOLD,
        NATURAL_HOLD_BUDGET,
        WINDOW_BELOW_50,
        PROTECTION_VIOLATION,
    ]
    .contains(&reason)
}

/// Natural hold budget: one percent of the measurement interval.
pub fn hold_budget_us(duration_us: u64) -> u64 {
    duration_us / 100
}

/// The most recently accepted input, with the age bound frozen at arrival.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AcceptedInput {
    pub received_us: u64,
    pub age_high_us: i128,
}

/// Protection inputs as the clock and session code currently see them. Times
/// are anchors in native monotonic microseconds, so every value can be aged to
/// the moment of evaluation. `None` means no evidence exists yet, which is never
/// a violation.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Protection {
    /// Send time of the oldest unanswered clock exchange.
    pub pending_since_us: Option<u64>,
    /// Receive time of the last accepted clock reply.
    pub last_sync_us: Option<u64>,
    /// The clock error estimated from the retained exchanges is above 200 ppm
    /// (slope of the offset midpoints), or two retained exchanges cannot both
    /// hold under that bound (disjoint offset intervals). See
    /// `ClockWindow::drift_estimate_exceeded` for the exact definition.
    pub drift_violation: bool,
    /// Last input the receiver accepted; its age keeps growing while nothing
    /// new is accepted, whether or not a result is being published.
    pub input: Option<AcceptedInput>,
}

impl Protection {
    /// Upper age bound of the last accepted input at `now`.
    pub fn input_age_high_us(&self, now: u64) -> Option<i128> {
        self.input
            .map(|i| i.age_high_us + i128::from(now.saturating_sub(i.received_us)))
    }

    /// Unanswered clock exchange age at `now`.
    pub fn rtt_us(&self, now: u64) -> Option<u64> {
        self.pending_since_us.map(|at| now.saturating_sub(at))
    }

    /// Time since the last accepted clock reply at `now`.
    pub fn sync_age_us(&self, now: u64) -> Option<u64> {
        self.last_sync_us.map(|at| now.saturating_sub(at))
    }

    pub fn violated(&self, now: u64) -> bool {
        self.input_age_high_us(now)
            .is_some_and(|v| v > AGE_LIMIT_US)
            || self.rtt_us(now).is_some_and(|v| v > RTT_LIMIT_US)
            || self.sync_age_us(now).is_some_and(|v| v > SYNC_LIMIT_US)
            || self.drift_violation
    }
}

pub struct Observation<'a> {
    pub now: u64,
    /// `(start_us, end_us)` once the measurement interval is known.
    pub interval: Option<(u64, u64)>,
    pub hold_started: Option<u64>,
    pub last_hold: Option<(u64, u64)>,
    pub samples: &'a [Sample],
    pub protection: Protection,
    /// Set when a result was refused at publication because a protection
    /// limit was already violated at that moment.
    pub publish_refused_at: Option<u64>,
}

#[derive(Default)]
pub struct OnlineGuard {
    budget_us: u64,
    ongoing: Option<u64>,
    seen_last: Option<(u64, u64)>,
    holds: Vec<(u64, u64)>,
    next_scan: Option<u64>,
    checked_samples: usize,
}

impl OnlineGuard {
    pub fn new(duration_us: u64) -> Self {
        Self {
            budget_us: hold_budget_us(duration_us),
            ..Self::default()
        }
    }

    fn track_holds(&mut self, o: &Observation) {
        if o.last_hold != self.seen_last
            && let Some(done) = o.last_hold
        {
            self.holds.push(done);
        }
        self.seen_last = o.last_hold;
        self.ongoing = o.hold_started;
    }

    /// First violated condition, in the fixed order A11, A12, A13, A14. Hold and
    /// window limits need the measurement interval; the protection limits do
    /// not and are judged on every pass from the first one.
    pub fn observe(&mut self, o: &Observation) -> Option<&'static str> {
        self.track_holds(o);
        if let Some((start, end)) = o.interval
            && let Some(code) = self.interval_violation(o, start, end)
        {
            return Some(code);
        }
        let fresh = o.samples.get(self.checked_samples..).unwrap_or_default();
        self.checked_samples = o.samples.len();
        let stale = fresh.iter().any(|s| s.age_high_us > AGE_LIMIT_US);
        (stale || o.publish_refused_at.is_some() || o.protection.violated(o.now))
            .then_some(PROTECTION_VIOLATION)
    }

    fn interval_violation(
        &mut self,
        o: &Observation,
        start: u64,
        end: u64,
    ) -> Option<&'static str> {
        let mut natural = 0u64;
        let mut longest = 0u64;
        let spans = self
            .holds
            .iter()
            .map(|&(began, recovered)| (began, Some(recovered)))
            .chain(self.ongoing.map(|began| (began, None)));
        for (began, recovered) in spans {
            if recovered.is_some_and(|r| r <= start) || began >= end {
                continue;
            }
            let reach = recovered.unwrap_or(o.now);
            longest = longest.max(reach.saturating_sub(began));
            let clipped = recovered.unwrap_or(o.now.min(end)).min(end);
            natural = natural.saturating_add(clipped.saturating_sub(began.max(start)));
        }
        if longest >= CONTINUOUS_HOLD_US {
            return Some(CONTINUOUS_HOLD);
        }
        if natural > self.budget_us {
            return Some(NATURAL_HOLD_BUDGET);
        }
        (self.scan_due(o.now) && min_closed_window(o.samples, start, end, o.now) < MIN_RECOGNITIONS)
            .then_some(WINDOW_BELOW_50)
    }

    fn scan_due(&mut self, now: u64) -> bool {
        let next = *self.next_scan.get_or_insert(now);
        if now < next {
            return false;
        }
        let mut following = next.saturating_add(TICK_US);
        if following <= now {
            following = now.saturating_add(TICK_US); // Late: skip, never burst.
        }
        self.next_scan = Some(following);
        true
    }
}

/// Smallest recognition count over every closed window `[t, t + 10 s)` with
/// `start <= t` and `t + 10 s <= min(now, end)`, for any integer `t`. The count
/// only falls right after a result time, so `start` and each `p + 1` suffice.
/// `usize::MAX` when no window is closed yet.
pub fn min_closed_window(samples: &[Sample], start: u64, end: u64, now: u64) -> usize {
    let Some(last_start) = now.min(end).checked_sub(WINDOW_US) else {
        return usize::MAX;
    };
    if last_start < start {
        return usize::MAX;
    }
    let mut times: Vec<u64> = samples
        .iter()
        .map(|s| s.published_us)
        .filter(|p| (start..end).contains(p))
        .collect();
    times.sort_unstable();
    let count =
        |t: u64| times.partition_point(|p| *p < t + WINDOW_US) - times.partition_point(|p| *p < t);
    std::iter::once(start)
        .chain(
            times
                .iter()
                .map(|p| p + 1)
                .filter(|t| (start..=last_start).contains(t)),
        )
        .map(count)
        .min()
        .unwrap_or(usize::MAX)
}

#[cfg(test)]
mod tests;
