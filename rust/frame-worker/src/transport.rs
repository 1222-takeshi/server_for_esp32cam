//! Optional plain LAN WebSocket transport. One accepted socket per owner;
//! session/hello/clock deadlines, rate limits and camera ownership belong above.
use crate::{MAX_GENERATION, MAX_JPEG};
use sha2::{Digest, Sha256};
use std::{
    io::{self, Read, Write},
    net::{Shutdown, TcpStream},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;
pub use tungstenite::Message;
use tungstenite::{
    WebSocket, accept_hdr_with_config,
    handshake::server::{Request, Response},
    protocol::WebSocketConfig,
};

pub use crate::control::TEXT_LIMIT;
const HANDSHAKE_LIMIT: usize = 8192;
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "WebSocket rejected")
}

/// No Debug implementation: credentials and request headers must never be logged.
pub struct Auth {
    path: String,
    key_hash: [u8; 32],
}
impl Auth {
    pub fn new(camera: &str, key: &[u8]) -> io::Result<Self> {
        if camera.is_empty()
            || camera.len() > 64
            || !camera
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            || key.is_empty()
            || key.len() > 512
            || !key.iter().all(|b| (33..=126).contains(b))
        {
            return Err(invalid());
        }
        Ok(Self {
            path: format!("/ws/cameras/{camera}/live"),
            key_hash: Sha256::digest(key).into(),
        })
    }
    fn authorized(&self, request: &Request) -> bool {
        let mut headers = request.headers().get_all("x-api-key").iter();
        let Some(key) = headers.next() else {
            return false;
        };
        headers.next().is_none()
            && key.as_bytes().len() <= 512
            && bool::from(
                self.key_hash
                    .ct_eq(Sha256::digest(key.as_bytes()).as_slice()),
            )
    }
}

struct BoundedStream {
    socket: TcpStream,
    deadline: Option<Instant>,
    remaining: Option<usize>,
    write_failed: bool,
    quickack: bool,
    quickack_requests: u64,
}
impl BoundedStream {
    fn quick_ack(&mut self) -> io::Result<()> {
        if !self.quickack {
            return Ok(());
        }
        #[cfg(target_os = "linux")]
        {
            // QUICKACK is not persistent: re-request after each positive TCP read.
            // Convert errors to a fatal kind; bytes already consumed cannot be retried.
            socket2::SockRef::from(&self.socket)
                .set_tcp_quickack(true)
                .map_err(|_| io::Error::other("TCP acknowledgement option failed"))?;
            self.quickack_requests = self.quickack_requests.saturating_add(1);
            Ok(())
        }
        #[cfg(not(target_os = "linux"))]
        {
            Err(io::ErrorKind::Unsupported.into())
        }
    }
}
impl Read for BoundedStream {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let length = if let Some(deadline) = self.deadline {
            let remaining = deadline
                .checked_duration_since(Instant::now())
                .filter(|d| !d.is_zero())
                .ok_or_else(|| io::Error::from(io::ErrorKind::TimedOut))?;
            if self.remaining == Some(0) {
                return Err(invalid());
            }
            self.socket.set_read_timeout(Some(remaining))?;
            self.remaining
                .map_or(buffer.len(), |cap| buffer.len().min(cap))
        } else {
            buffer.len()
        };
        let count = self.socket.read(&mut buffer[..length])?;
        if let Some(remaining) = self.remaining.as_mut() {
            *remaining -= count;
        }
        if count > 0 {
            self.quick_ack()?;
        }
        Ok(count)
    }
}
impl Write for BoundedStream {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        let result = (|| {
            let timeout = if let Some(deadline) = self.deadline {
                deadline
                    .checked_duration_since(Instant::now())
                    .filter(|d| !d.is_zero())
                    .ok_or_else(|| io::Error::from(io::ErrorKind::TimedOut))?
                    .min(Duration::from_millis(200))
            } else {
                Duration::from_millis(200)
            };
            self.socket.set_write_timeout(Some(timeout))?;
            self.socket.write(buffer)
        })();
        if result.as_ref().is_err_and(|e| {
            matches!(
                e.kind(),
                io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
            )
        }) {
            self.write_failed = true;
        }
        result
    }
    fn flush(&mut self) -> io::Result<()> {
        self.socket.flush()
    }
}

pub struct Connection(WebSocket<BoundedStream>);
impl Drop for Connection {
    fn drop(&mut self) {
        let _ = self.0.get_mut().socket.shutdown(Shutdown::Both);
    }
}
impl Connection {
    pub fn accept(socket: TcpStream, auth: &Auth) -> io::Result<Self> {
        Self::accept_with_quickack(socket, auth, false)
    }
    // Tungstenite Callback requires its unboxed HTTP ErrorResponse.
    #[allow(clippy::result_large_err)]
    pub fn accept_with_quickack(
        socket: TcpStream,
        auth: &Auth,
        quickack: bool,
    ) -> io::Result<Self> {
        #[cfg(not(target_os = "linux"))]
        if quickack {
            return Err(io::ErrorKind::Unsupported.into());
        }
        socket.set_nodelay(true)?;
        let stream = BoundedStream {
            socket,
            deadline: Some(Instant::now() + HANDSHAKE_TIMEOUT),
            remaining: Some(HANDSHAKE_LIMIT),
            write_failed: false,
            quickack,
            quickack_requests: 0,
        };
        let config = WebSocketConfig::default()
            .read_buffer_size(4096)
            .write_buffer_size(0)
            .max_write_buffer_size(16 * 1024)
            .max_message_size(Some(MAX_JPEG + 48))
            .max_frame_size(Some(MAX_JPEG + 48));
        let callback = |request: &Request, response: Response| {
            let status =
                if request.uri().path_and_query().map(|p| p.as_str()) != Some(auth.path.as_str()) {
                    404
                } else if !auth.authorized(request) {
                    403
                } else {
                    return Ok(response);
                };
            Err(tungstenite::http::Response::builder()
                .status(status)
                .body(None)
                .unwrap())
        };
        let mut socket =
            accept_hdr_with_config(stream, callback, Some(config)).map_err(|_| invalid())?;
        socket.get_mut().deadline = None;
        socket.get_mut().remaining = None;
        socket
            .get_mut()
            .socket
            .set_read_timeout(Some(Duration::from_millis(50)))?;
        Ok(Self(socket))
    }
    pub fn quickack_requested(&self) -> bool {
        self.0.get_ref().quickack
    }
    pub fn quickack_requests(&self) -> u64 {
        self.0.get_ref().quickack_requests
    }
    /// None is a 50ms absolute receive wakeup, not EOF. Drop on any error.
    pub fn receive(&mut self) -> io::Result<Option<Message>> {
        if self.0.get_mut().write_failed {
            return Err(invalid());
        }
        self.0.get_mut().deadline = Some(Instant::now() + Duration::from_millis(50));
        let received = self.0.read();
        self.finish_receive(received)
    }
    fn finish_receive(
        &mut self,
        received: tungstenite::Result<Message>,
    ) -> io::Result<Option<Message>> {
        if self.0.get_mut().write_failed {
            return Err(invalid());
        }
        match received {
            Ok(Message::Text(text)) if text.len() > TEXT_LIMIT => Err(invalid()),
            Ok(Message::Close(_)) => Err(io::ErrorKind::ConnectionAborted.into()),
            Ok(message) => Ok(Some(message)),
            Err(tungstenite::Error::Io(e))
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                ) =>
            {
                Ok(None)
            }
            Err(_) => Err(invalid()),
        }
    }
    pub fn shutdown(&mut self) {
        let _ = self.0.get_mut().socket.shutdown(Shutdown::Both);
    }
    pub fn send_text(&mut self, text: &str) -> io::Result<()> {
        if text.len() > TEXT_LIMIT {
            return Err(invalid());
        }
        self.0.get_mut().deadline = Some(Instant::now() + Duration::from_millis(200));
        self.0
            .send(Message::Text(text.into()))
            .map_err(|_| invalid())
    }
}

pub fn nonce() -> io::Result<String> {
    let mut bytes = [0u8; 8];
    getrandom::fill(&mut bytes).map_err(|_| io::Error::other("entropy unavailable"))?;
    Ok(format!("{:016x}", u64::from_be_bytes(bytes)))
}
pub fn initial_generation() -> io::Result<u64> {
    let mut bytes = [0u8; 8];
    getrandom::fill(&mut bytes).map_err(|_| io::Error::other("entropy unavailable"))?;
    let value = (u64::from_be_bytes(bytes) & ((1 << 40) - 1)) + 1;
    debug_assert!(value <= MAX_GENERATION);
    Ok(value)
}

#[cfg(test)]
mod tests;
