//! Opt-in, current-thread-only scheduling for finite experiments.
use std::{io, marker::PhantomData, rc::Rc};

/// Keep this handle on the creating thread. Future children of the I/O thread
/// would inherit CPU0; the external guardian must reject unexpected task masks.
pub struct CpuIsolation {
    enabled: bool,
    _same_thread: PhantomData<Rc<()>>,
}
impl CpuIsolation {
    /// Call before creating monitor, ORT or inference worker threads.
    pub fn prepare(enabled: bool) -> io::Result<Self> {
        if enabled {
            #[cfg(target_os = "linux")]
            {
                let allowed = rustix::thread::sched_getaffinity(None)?;
                if !(0..4).all(|cpu| allowed.is_set(cpu)) {
                    return Err(io::Error::other("CPU0..3 must all be allowed"));
                }
                pin(&[1, 2, 3])?;
            }
            #[cfg(not(target_os = "linux"))]
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "CPU isolation requires Linux",
            ));
        }
        Ok(Self {
            enabled,
            _same_thread: PhantomData,
        })
    }
    /// Call on the same thread, after Worker creation and before listening.
    pub fn pin_io(&self) -> io::Result<()> {
        if self.enabled {
            #[cfg(target_os = "linux")]
            pin(&[0])?;
        }
        Ok(())
    }
    pub fn name(&self) -> &'static str {
        if self.enabled {
            "io0_inference1_3"
        } else {
            "default"
        }
    }
}
#[cfg(target_os = "linux")]
fn pin(cpus: &[usize]) -> io::Result<()> {
    use rustix::thread::{CpuSet, sched_getaffinity, sched_setaffinity};
    let mut requested = CpuSet::new();
    for &cpu in cpus {
        requested.set(cpu);
    }
    sched_setaffinity(None, &requested)?;
    let actual = sched_getaffinity(None)?;
    if actual.count() as usize != cpus.len() || !cpus.iter().all(|&cpu| actual.is_set(cpu)) {
        return Err(io::Error::other("CPU affinity readback mismatch"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn default_is_a_noop_and_unsupported_request_fails() {
        let policy = CpuIsolation::prepare(false).unwrap();
        assert_eq!(policy.name(), "default");
        policy.pin_io().unwrap();
        #[cfg(not(target_os = "linux"))]
        assert_eq!(
            CpuIsolation::prepare(true).err().unwrap().kind(),
            io::ErrorKind::Unsupported
        );
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn inherited_inference_mask_survives_moving_only_its_creator() {
        use rustix::thread::{sched_getaffinity, sched_setaffinity};
        let caller = sched_getaffinity(None).unwrap();
        std::thread::spawn(|| {
            let original = sched_getaffinity(None).unwrap();
            if !(0..4).all(|cpu| original.is_set(cpu)) {
                assert!(CpuIsolation::prepare(true).is_err());
                assert_eq!(sched_getaffinity(None).unwrap().count(), original.count());
                return; // Portable CI can expose fewer than four allowed CPUs.
            }
            let policy = CpuIsolation::prepare(true).unwrap();
            assert_eq!(policy.name(), "io0_inference1_3");
            let (tx, rx) = std::sync::mpsc::channel();
            let worker = std::thread::spawn(move || {
                rx.recv().unwrap();
                let mask = sched_getaffinity(None).unwrap();
                assert_eq!(mask.count(), 3);
                assert!((1..4).all(|cpu| mask.is_set(cpu)));
            });
            policy.pin_io().unwrap();
            let io = sched_getaffinity(None).unwrap();
            assert_eq!(io.count(), 1);
            assert!(io.is_set(0));
            // An already restricted caller must not be widened by prepare.
            assert!(CpuIsolation::prepare(true).is_err());
            assert_eq!(sched_getaffinity(None).unwrap().count(), 1);
            assert!(sched_getaffinity(None).unwrap().is_set(0));
            tx.send(()).unwrap();
            worker.join().unwrap();
            // Only this disposable test thread changed affinity.
            sched_setaffinity(None, &original).unwrap();
        })
        .join()
        .unwrap();
        let after = sched_getaffinity(None).unwrap();
        assert_eq!(caller.count(), after.count());
        assert!(
            (0..rustix::thread::CpuSet::MAX_CPU).all(|cpu| caller.is_set(cpu) == after.is_set(cpu))
        );
    }
}
