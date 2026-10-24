//! One inference thread + one latest waiting frame. No disk or network I/O.
//! Finite live experiments only: a supervisor must enforce a process deadline
//! because an in-flight native inference cannot be forcibly cancelled in Rust.
use crate::{
    Frame, LatestSlot, MAX_JPEG,
    control::Arrival,
    detector::Detection,
    operating_mode::{ModeError, OperatingMode},
};
use std::{
    cell::Cell,
    io,
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::Duration,
};

pub type Clock = Arc<dyn Fn() -> u64 + Send + Sync>;
const MAX_SAMPLES: usize = 8192;
pub struct Job {
    pub frame: Frame,
    pub arrival: Arrival,
    pub cancelled: Arc<AtomicBool>,
}
pub struct Completed {
    pub frame: Frame,
    pub detections: Vec<Detection>,
    pub published_us: u64,
    pub compute_us: u64,
    arrival: Arrival,
}
impl Completed {
    pub fn age_bounds(&self, now: u64) -> Option<(i128, i128)> {
        self.arrival.age_bounds(&self.frame, now)
    }
}
pub struct DropSample {
    pub boot: u64,
    pub generation: Option<u64>,
    pub sequence: u64,
    pub at_us: u64,
    pub stage: &'static str,
    pub age_high_us: Option<i128>,
    pub arrival_age_high_us: Option<i128>,
    pub wait_us: Option<u64>,
    pub compute_us: u64,
}
#[derive(Default)]
pub struct Counters {
    pub inferred: u64,
    pub published: u64,
    pub dropped: u64,
    pub failures: u64,
    pub evidence_lost: u64,
}
#[derive(Clone, Copy)]
pub struct Sample {
    pub boot: u64,
    pub generation: u64,
    pub sequence: u64,
    pub published_us: u64,
    pub age_high_us: i128,
    pub compute_us: u64,
}
pub struct State {
    clock: Clock,
    #[cfg(feature = "transport")]
    session: Option<Arc<AtomicBool>>,
    pub mode: OperatingMode,
    pub counters: Counters,
    latest: Option<Arc<Completed>>,
    samples: Vec<Sample>,
    drops: std::collections::VecDeque<DropSample>,
}
impl State {
    pub fn new(generation: u64, clock: Clock) -> Result<Self, ModeError> {
        let copy = clock.clone();
        let mode = OperatingMode::new(generation, move || copy())?;
        Ok(Self {
            clock,
            #[cfg(feature = "transport")]
            session: None,
            mode,
            counters: Counters::default(),
            latest: None,
            samples: Vec::new(),
            drops: std::collections::VecDeque::with_capacity(32),
        })
    }
    pub fn now(&self) -> u64 {
        (self.clock)()
    }
    #[cfg(feature = "transport")]
    pub(crate) fn claim_session(&mut self) -> Result<Arc<AtomicBool>, &'static str> {
        if self.session.is_some() || self.mode.connected() {
            return Err("camera_busy");
        }
        let token = Arc::new(AtomicBool::new(false));
        self.session = Some(token.clone());
        Ok(token)
    }
    #[cfg(feature = "transport")]
    pub(crate) fn release_session(&mut self, token: &Arc<AtomicBool>) {
        if self
            .session
            .as_ref()
            .is_some_and(|owner| Arc::ptr_eq(owner, token))
        {
            let _ = self.disconnect(token);
            self.session = None;
        }
    }
    #[cfg(test)]
    pub(crate) fn record_test_sample(&mut self, sample: Sample) {
        self.samples.push(sample);
    }
    pub fn samples(&self) -> &[Sample] {
        &self.samples
    }
    pub fn drops(&self) -> &std::collections::VecDeque<DropSample> {
        &self.drops
    }
    fn dropped(&mut self, job: &Job, stage: &'static str, now: u64, compute_us: u64) {
        self.counters.dropped = self.counters.dropped.saturating_add(1);
        if self.drops.len() == 32 {
            self.drops.pop_front();
        }
        self.drops.push_back(DropSample {
            boot: job.frame.boot,
            generation: job.frame.generation,
            sequence: job.frame.sequence,
            at_us: now,
            stage,
            age_high_us: job.arrival.age_bounds(&job.frame, now).map(|b| b.1),
            arrival_age_high_us: job
                .arrival
                .age_bounds(&job.frame, job.arrival.received_us())
                .map(|b| b.1),
            wait_us: now.checked_sub(job.arrival.received_us()),
            compute_us,
        });
    }
    pub fn latest(&mut self) -> Result<Option<Arc<Completed>>, ModeError> {
        self.mode.tick()?;
        let now = (self.clock)();
        Ok(self
            .latest
            .as_ref()
            .filter(|r| self.mode.allows(&r.frame) && r.arrival.fits(&r.frame, now, 0))
            .cloned())
    }
    /// Fence cancellation and disconnect with the same lock as publication.
    pub fn disconnect(&mut self, token: &AtomicBool) -> Result<(), ModeError> {
        token.store(true, Ordering::Release);
        self.latest = None;
        self.mode.disconnect()
    }
}
#[derive(Debug, PartialEq, Eq)]
pub enum Submit {
    Queued,
    Overwritten,
    Busy,
    Stopped,
    Invalid,
}
pub struct Worker {
    state: Arc<Mutex<State>>,
    queue: Arc<LatestSlot<Job>>,
    stopped: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}
impl Worker {
    pub fn start(
        state: Arc<Mutex<State>>,
        mut infer: impl FnMut(&[u8]) -> Result<Vec<Detection>, &'static str> + Send + 'static,
    ) -> io::Result<Self> {
        let clock = state
            .lock()
            .map_err(|_| io::Error::other("state unavailable"))?
            .clock
            .clone();
        let queue = Arc::new(LatestSlot::<Job>::default());
        let stopped = Arc::new(AtomicBool::new(false));
        let (waiting, stop) = (queue.clone(), stopped.clone());
        let owner_state = state.clone();
        let handle = thread::Builder::new()
            .name("frame-inference".into())
            .spawn(move || {
                while !stop.load(Ordering::Acquire) {
                    let Some(job) = waiting.take() else {
                        thread::park_timeout(Duration::from_millis(100));
                        continue;
                    };
                    {
                        let Ok(mut s) = state.lock() else {
                            break;
                        };
                        let now = clock();
                        let reason = if job.cancelled.load(Ordering::Acquire) {
                            Some("cancelled_before")
                        } else if !s.mode.allows(&job.frame) {
                            Some("mode_before")
                        } else if !job.arrival.fits(&job.frame, now, 165_000) {
                            Some("age_before")
                        } else {
                            None
                        };
                        if let Some(reason) = reason {
                            s.dropped(&job, reason, now, 0);
                            continue;
                        }
                    }
                    let begun = clock();
                    let output = catch_unwind(AssertUnwindSafe(|| infer(&job.frame.jpeg)))
                        .ok()
                        .and_then(Result::ok);
                    let compute_us = clock().saturating_sub(begun);
                    let Ok(mut s) = state.lock() else {
                        break;
                    };
                    s.counters.inferred = s.counters.inferred.saturating_add(1);
                    if stop.load(Ordering::Acquire)
                        || job.cancelled.load(Ordering::Acquire)
                        || !s.mode.allows(&job.frame)
                    {
                        let reason = if stop.load(Ordering::Acquire)
                            || job.cancelled.load(Ordering::Acquire)
                        {
                            "cancelled_after"
                        } else {
                            "mode_after"
                        };
                        s.dropped(&job, reason, clock(), compute_us);
                        continue;
                    }
                    let Some(detections) = output.filter(|v| {
                        v.len() <= 300
                            && v.iter().all(|d| {
                                d.score.is_finite()
                                    && (0.0..=1.0).contains(&d.score)
                                    && d.bbox
                                        .iter()
                                        .all(|n| n.is_finite() && (0.0..=1.0).contains(n))
                                    && d.bbox[0] < d.bbox[2]
                                    && d.bbox[1] < d.bbox[3]
                            })
                    }) else {
                        s.counters.failures = s.counters.failures.saturating_add(1);
                        let _ = s.mode.protect("inference_failed");
                        continue;
                    };
                    let frame = Frame {
                        jpeg: vec![],
                        ..job.frame
                    };
                    let arrival = job.arrival;
                    let mut completed = Arc::new(Completed {
                        frame,
                        detections,
                        published_us: 0,
                        compute_us,
                        arrival,
                    });
                    let State {
                        mode,
                        latest,
                        counters,
                        samples,
                        ..
                    } = &mut *s;
                    let published_at = Cell::new(0);
                    let published = mode
                        .publish_if(
                            &job.frame,
                            |now| {
                                published_at.set(now);
                                !stop.load(Ordering::Acquire)
                                    && !job.cancelled.load(Ordering::Acquire)
                                    && arrival.fits(&job.frame, published_at.get(), 0)
                            },
                            || {
                                Arc::get_mut(&mut completed).unwrap().published_us =
                                    published_at.get();
                                *latest = Some(completed.clone());
                                if samples.len() < MAX_SAMPLES {
                                    samples.push(Sample {
                                        boot: completed.frame.boot,
                                        generation: completed.frame.generation.unwrap(),
                                        sequence: completed.frame.sequence,
                                        published_us: completed.published_us,
                                        age_high_us: completed
                                            .age_bounds(completed.published_us)
                                            .unwrap()
                                            .1,
                                        compute_us,
                                    });
                                } else {
                                    counters.evidence_lost =
                                        counters.evidence_lost.saturating_add(1);
                                }
                                true
                            },
                        )
                        .unwrap_or(false);
                    if published {
                        counters.published = counters.published.saturating_add(1);
                    } else {
                        let now = if published_at.get() > 0 {
                            published_at.get()
                        } else {
                            clock()
                        };
                        s.dropped(&job, "publication_gate", now, compute_us);
                    }
                }
                stop.store(true, Ordering::Release);
            })?;
        Ok(Self {
            state: owner_state,
            queue,
            stopped,
            thread: Some(handle),
        })
    }
    pub fn state(&self) -> Arc<Mutex<State>> {
        self.state.clone()
    }
    pub fn submit(&self, job: Job) -> Submit {
        if self.stopped.load(Ordering::Acquire) {
            return Submit::Stopped;
        }
        if !job.arrival.matches(&job.frame)
            || job.frame.jpeg.is_empty()
            || job.frame.jpeg.len() > MAX_JPEG
        {
            return Submit::Invalid;
        }
        let result = match self.queue.replace(job) {
            Ok(None) => Submit::Queued,
            Ok(Some(_)) => Submit::Overwritten,
            Err(_) => Submit::Busy,
        };
        self.thread.as_ref().unwrap().thread().unpark();
        result
    }
    /// May wait for a native call. The process supervisor supplies the hard limit.
    pub fn stop(mut self) -> thread::Result<()> {
        self.stopped.store(true, Ordering::Release);
        let handle = self.thread.take().unwrap();
        handle.thread().unpark();
        handle.join()
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Release);
        if let Some(handle) = &self.thread {
            handle.thread().unpark();
        }
    }
}

#[cfg(test)]
mod tests;
