//! Host-minus-device uncertainty. Monotonic microseconds, never wall time.
//! The 200ppm drift bound is an assumption to validate on the camera.
use crate::Frame;
use std::collections::VecDeque;

const MAX_RTT_US: u64 = 1_000_000;
const MAX_AGE_US: u64 = 10_000_000;
const DRIFT_PPM: i128 = 200;

pub type AgeBounds = (i128, i128);

#[derive(Debug, Clone, Copy)]
pub struct ClockSample {
    boot: u64,
    low: i128,
    high: i128,
    received_us: u64,
    device_us: u64,
}

impl ClockSample {
    pub fn from_exchange(
        boot: u64,
        device_us: u64,
        sent_us: u64,
        received_us: u64,
    ) -> Result<Self, &'static str> {
        if [boot, device_us, sent_us, received_us].contains(&0)
            || received_us
                .checked_sub(sent_us)
                .is_none_or(|rtt| rtt > MAX_RTT_US)
        {
            return Err("invalid clock exchange");
        }
        Ok(Self {
            boot,
            low: i128::from(sent_us) - i128::from(device_us),
            high: i128::from(received_us) - i128::from(device_us),
            received_us,
            device_us,
        })
    }

    /// Offset intervals that stay disjoint after widening by the assumed drift
    /// over the device time between the two exchanges contradict that bound.
    fn contradicts(&self, other: &Self) -> bool {
        let delta = i128::from(self.device_us.abs_diff(other.device_us));
        let margin = (delta * DRIFT_PPM + 999_999) / 1_000_000;
        self.low - other.high > margin || other.low - self.high > margin
    }

    /// Slope of the offset midpoints of two exchanges of one boot, judged against
    /// the 200 ppm bound. `None` when the estimate is not defined for this pair.
    ///
    /// The offset midpoint of an exchange is `(low + high) / 2`, so its error
    /// is at most half the interval width (the round trip time). The slope
    /// between two midpoints is therefore uncertain by at most
    /// `(width_a + width_b) / 2` over the device time `d` between them. The
    /// estimate is defined only when that worst case, in ppm, does not exceed
    /// the 200 ppm limit (and `d > 0`); a shorter baseline is dominated by the
    /// round trip width, not by clock error, and is left undefined rather than
    /// being read as drift. All arithmetic is exact: both sides are scaled so
    /// that no division (and no rounding) is involved.
    fn midpoint_drift_exceeds(&self, other: &Self) -> Option<bool> {
        let (a, b) = if self.device_us <= other.device_us {
            (self, other)
        } else {
            (other, self)
        };
        let device = i128::from(b.device_us - a.device_us);
        let widths = (a.high - a.low) + (b.high - b.low);
        // widths / 2 / device * 1e6 <= 200  <=>  widths * 5000 <= 2 * device
        if a.boot != b.boot || device == 0 || widths * 5_000 > 2 * device {
            return None;
        }
        // |slope| > 200 ppm. `doubled` is twice the midpoint difference:
        // (doubled / 2) * 1e6 > 200 * device  <=>  doubled * 2500 > device
        let doubled = ((b.low + b.high) - (a.low + a.high)).abs();
        Some(doubled * 2_500 > device)
    }

    pub fn age_bounds(&self, frame: &Frame, now_us: u64) -> Option<AgeBounds> {
        if now_us == 0 || frame.boot != self.boot || now_us.abs_diff(self.received_us) > MAX_AGE_US
        {
            return None;
        }
        // i128 covers all u64 timestamps, signed offsets and drift products.
        // Captures preceding the exchange accumulate drift too.
        let delta = i128::from(frame.captured_us.abs_diff(self.device_us));
        let margin = (delta * DRIFT_PPM + 999_999) / 1_000_000;
        let elapsed = i128::from(now_us) - i128::from(frame.captured_us);
        let low = elapsed - self.high - margin;
        let high = elapsed - self.low + margin;
        (high >= 0).then_some((low.max(0), high))
    }
}

/// Single owner; at most eight samples. Each retains its original expiry.
#[derive(Default)]
pub struct ClockWindow {
    samples: VecDeque<ClockSample>,
}

impl ClockWindow {
    pub fn add(&mut self, sample: ClockSample) -> Result<(), &'static str> {
        if let Some(last) = self.samples.back() {
            if sample.received_us < last.received_us {
                return Err("out-of-order clock exchange");
            }
            if sample.boot != last.boot {
                self.samples.clear();
            }
        }
        if self.samples.len() == 8 {
            self.samples.pop_front();
        }
        self.samples.push_back(sample);
        Ok(())
    }

    /// Bounded numeric evidence only: boot, device, host sent, host received.
    pub fn evidence(&self) -> Vec<[u64; 4]> {
        self.samples
            .iter()
            .map(|s| {
                [
                    s.boot,
                    s.device_us,
                    (s.low + i128::from(s.device_us)) as u64,
                    s.received_us,
                ]
            })
            .collect()
    }

    /// True when two retained samples of one boot cannot both hold under the
    /// assumed drift bound: their offset intervals stay disjoint after
    /// widening by 200 ppm of the device time between them. This is a
    /// consistency check on the intervals and fires only once they disagree by
    /// more than the bound plus their own uncertainty; it is kept as an
    /// additional check next to [`Self::drift_estimate_exceeded`]. Read-only;
    /// it never changes admission decisions.
    pub fn drift_inconsistent(&self) -> bool {
        self.pairs().any(|(a, b)| a.contradicts(b))
    }

    /// True when the clock error estimated from the retained samples is above
    /// 200 ppm: for some pair of same-boot samples the slope of the offset
    /// midpoints exceeds 200 ppm (strictly; exactly 200 ppm is allowed) and the
    /// pair is long enough for that slope to be meaningful (see
    /// `ClockSample::midpoint_drift_exceeds`). Every retained pair is
    /// considered, so the longest baseline of up to eight exchanges is
    /// included. With fewer than two samples, or only pairs too short to
    /// resolve 200 ppm, no estimate exists and the result is `false` (the
    /// value is still computed on every call, so it is available whenever
    /// evidence exists). This is a point estimate over interval midpoints, not
    /// an adjusted fit of the clock model, and it is no claim of equivalence to
    /// anything beyond that definition. Read-only.
    pub fn drift_estimate_exceeded(&self) -> bool {
        self.pairs()
            .any(|(a, b)| a.midpoint_drift_exceeds(b) == Some(true))
    }

    fn pairs(&self) -> impl Iterator<Item = (&ClockSample, &ClockSample)> {
        self.samples.iter().enumerate().flat_map(move |(i, a)| {
            self.samples
                .iter()
                .skip(i + 1)
                .filter(move |b| a.boot == b.boot)
                .map(move |b| (a, b))
        })
    }

    pub fn age_bounds(&self, frame: &Frame, now_us: u64) -> Option<AgeBounds> {
        let mut bounds: Option<AgeBounds> = None;
        for sample in &self.samples {
            if sample.boot != frame.boot
                || sample.received_us > now_us
                || now_us - sample.received_us > MAX_AGE_US
            {
                continue;
            }
            let (low, high) = sample.age_bounds(frame, now_us)?;
            bounds = Some(match bounds {
                None => (low, high),
                Some((a, b)) => (low.max(a), high.min(b)),
            });
        }
        bounds.filter(|(low, high)| low <= high)
    }
}

#[cfg(test)]
mod tests;
