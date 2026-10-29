//! Diagnostic evidence only. Never supplies a freshness clock or mode command.
pub const MAX_RECORDS: usize = 4096;
pub const MAX_RTT_NS: u64 = 1_000_000_000;
pub type Result<T> = std::result::Result<T, &'static str>;

#[derive(Clone, Copy)]
pub struct Stamp {
    pub mono: [u64; 2],
    pub raw: [u64; 2],
}
impl Stamp {
    fn valid(self) -> bool {
        [self.mono, self.raw]
            .iter()
            .all(|v| v[0] > 0 && v[0] <= v[1])
    }
}

pub struct Probe {
    boot: u64,
    pending: Option<(String, Stamp)>,
    last_device_us: u64,
    records: Vec<[u64; 9]>,
}
impl Probe {
    pub fn new(boot: u64) -> Result<Self> {
        if boot == 0 {
            return Err("invalid_boot");
        }
        Ok(Self {
            boot,
            pending: None,
            last_device_us: 0,
            records: Vec::with_capacity(MAX_RECORDS),
        })
    }
    pub fn pending(&self) -> bool {
        self.pending.is_some()
    }
    pub fn begin(&mut self, nonce: String, sent: Stamp) -> Result<()> {
        if self.pending()
            || self.records.len() == MAX_RECORDS
            || nonce.len() != 16
            || !nonce.bytes().all(|b| b.is_ascii_hexdigit())
            || !sent.valid()
            || self
                .records
                .last()
                .is_some_and(|last| sent.mono[0] < last[6] || sent.raw[0] < last[8])
        {
            return Err("invalid_probe_request");
        }
        self.pending = Some((nonce, sent));
        Ok(())
    }
    pub fn pong(&mut self, boot: u64, nonce: &str, device: u64, received: Stamp) -> Result<()> {
        let (expected, sent) = self.pending.as_ref().ok_or("unsolicited_pong")?;
        if boot != self.boot
            || nonce != expected
            || device <= self.last_device_us
            || !received.valid()
        {
            return Err("invalid_pong");
        }
        for (start, end) in [(sent.mono, received.mono), (sent.raw, received.raw)] {
            if start[1] > end[0]
                || end[1]
                    .checked_sub(start[0])
                    .is_none_or(|rtt| rtt > MAX_RTT_NS)
            {
                return Err("invalid_rtt");
            }
        }
        self.records.push([
            device,
            sent.mono[0],
            sent.mono[1],
            sent.raw[0],
            sent.raw[1],
            received.mono[0],
            received.mono[1],
            received.raw[0],
            received.raw[1],
        ]);
        self.last_device_us = device;
        self.pending = None;
        Ok(())
    }
    pub fn records(&self) -> &[[u64; 9]] {
        &self.records
    }
}

#[cfg(test)]
mod tests;
