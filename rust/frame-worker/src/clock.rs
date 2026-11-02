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
    /// assumed drift bound. This checks the consistency of the offset
    /// intervals; the code holds no estimate of the clock error, so it is not
    /// claimed to equal "estimated error above 200 ppm" (it can only fire
    /// once the measured intervals disagree by more than the bound plus their
    /// own uncertainty). Read-only; it never changes admission decisions.
    pub fn drift_inconsistent(&self) -> bool {
        self.samples.iter().enumerate().any(|(i, a)| {
            self.samples
                .iter()
                .skip(i + 1)
                .any(|b| a.boot == b.boot && a.contradicts(b))
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
