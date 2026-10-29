//! One immutable time domain per finite session. Never adjusts a system clock.
use std::time::Instant;
type Result<T> = std::result::Result<T, &'static str>;

#[derive(Clone)]
pub struct HostClock(Backend);

#[derive(Clone)]
enum Backend {
    Standard(Instant),
    #[cfg(target_os = "linux")]
    Raw(u64),
}
impl HostClock {
    pub fn new(raw: bool) -> Result<Self> {
        if !raw {
            return Ok(Self(Backend::Standard(Instant::now())));
        }
        #[cfg(target_os = "linux")]
        {
            Ok(Self(Backend::Raw(ns(rustix::time::ClockId::MonotonicRaw)?)))
        }
        #[cfg(not(target_os = "linux"))]
        Err("raw clock requires Linux")
    }
    pub fn name(&self) -> &'static str {
        match &self.0 {
            Backend::Standard(_) => "std_instant",
            #[cfg(target_os = "linux")]
            Backend::Raw(_) => "linux_monotonic_raw",
        }
    }
    pub fn epoch_ns(&self) -> Option<u64> {
        match &self.0 {
            Backend::Standard(_) => None,
            #[cfg(target_os = "linux")]
            Backend::Raw(epoch) => Some(*epoch),
        }
    }
    pub fn now_us(&self) -> Result<u64> {
        match &self.0 {
            Backend::Standard(epoch) => u64::try_from(epoch.elapsed().as_micros())
                .ok()
                .and_then(|n| n.checked_add(1))
                .ok_or("clock range"),
            #[cfg(target_os = "linux")]
            Backend::Raw(epoch) => {
                elapsed_us_from_ns(*epoch, ns(rustix::time::ClockId::MonotonicRaw)?)
            }
        }
    }
    /// Paired proof only for the opt-in source. Default has no extra OS reads.
    pub fn observations(&self) -> Result<Option<[u64; 4]>> {
        if self.epoch_ns().is_none() {
            return Ok(None);
        }
        #[cfg(target_os = "linux")]
        {
            snapshot().map(Some)
        }
        #[cfg(not(target_os = "linux"))]
        Err("raw clock requires Linux")
    }
}

#[cfg(any(target_os = "linux", test))]
fn elapsed_us_from_ns(epoch: u64, current: u64) -> Result<u64> {
    current
        .checked_sub(epoch)
        .map(|n| n / 1000 + 1)
        .ok_or("raw clock regressed")
}

#[cfg(target_os = "linux")]
fn ns(id: rustix::time::ClockId) -> Result<u64> {
    let t = rustix::time::clock_gettime(id);
    ns_parts(t.tv_sec, t.tv_nsec)
}

#[cfg(any(target_os = "linux", test))]
fn ns_parts(seconds: i64, nanos: i64) -> Result<u64> {
    let n = u64::try_from(nanos)
        .ok()
        .filter(|n| *n < 1_000_000_000)
        .ok_or("clock range")?;
    u64::try_from(seconds)
        .ok()
        .and_then(|s| s.checked_mul(1_000_000_000))
        .and_then(|s| s.checked_add(n))
        .filter(|s| *s > 0)
        .ok_or("clock range")
}

#[cfg(target_os = "linux")]
pub fn snapshot() -> Result<[u64; 4]> {
    use rustix::time::ClockId;
    let mono_low = ns(ClockId::Monotonic)?;
    let raw_low = ns(ClockId::MonotonicRaw)?;
    let raw_high = ns(ClockId::MonotonicRaw)?;
    let mono_high = ns(ClockId::Monotonic)?;
    Ok([mono_low, mono_high, raw_low, raw_high])
}

#[cfg(test)]
mod tests;
