use super::*;
use std::{net::TcpListener, thread};
use tungstenite::client::IntoClientRequest;

fn peer(
    path: &str,
    keys: &[&str],
) -> (
    std::result::Result<WebSocket<TcpStream>, ()>,
    thread::JoinHandle<io::Result<Connection>>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let owner = thread::spawn(move || {
        Connection::accept(listener.accept()?.0, &Auth::new("cam-1", b"test-key")?)
    });
    let socket = TcpStream::connect(address).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let mut request = format!("ws://{address}{path}")
        .into_client_request()
        .unwrap();
    for key in keys {
        request
            .headers_mut()
            .append("X-API-Key", key.parse().unwrap());
    }
    (
        tungstenite::client(request, socket)
            .map(|r| r.0)
            .map_err(|_| ()),
        owner,
    )
}

#[test]
fn exact_auth_and_path_required() {
    for (path, keys) in [
        ("/wrong", vec!["test-key"]),
        ("/ws/cameras/cam-1/live?x=1", vec!["test-key"]),
        ("/ws/cameras/cam-1/live", vec![]),
        ("/ws/cameras/cam-1/live", vec!["wrong-key"]),
        ("/ws/cameras/cam-1/live", vec!["test-key", "test-key"]),
    ] {
        let (client, owner) = peer(path, &keys);
        assert!(client.is_err());
        assert!(owner.join().unwrap().is_err());
    }
    for (camera, key) in [
        ("cam/1", b"key".as_slice()),
        ("cam-1", b""),
        ("cam-1", b"key\n"),
    ] {
        assert!(Auth::new(camera, key).is_err());
    }
}
#[test]
fn bounded_bidirectional_messages_and_idle_wakeup() {
    let (client, owner) = peer("/ws/cameras/cam-1/live", &["test-key"]);
    let mut client = client.unwrap();
    let mut server = owner.join().unwrap().unwrap();
    assert!(!server.quickack_requested());
    assert_eq!(server.quickack_requests(), 0);
    assert!(server.receive().unwrap().is_none());
    server.send_text("clock.ping").unwrap();
    assert_eq!(client.read().unwrap().into_text().unwrap(), "clock.ping");
    client
        .send(Message::Binary(vec![0; MAX_JPEG + 48].into()))
        .unwrap();
    assert!(
        matches!(server.receive().unwrap(), Some(Message::Binary(b)) if b.len() == MAX_JPEG+48)
    );
    client
        .send(Message::Text("x".repeat(TEXT_LIMIT).into()))
        .unwrap();
    assert!(matches!(server.receive().unwrap(), Some(Message::Text(t)) if t.len() == TEXT_LIMIT));
    assert!(server.send_text(&"x".repeat(TEXT_LIMIT + 1)).is_err());
    client
        .send(Message::Text("x".repeat(TEXT_LIMIT + 1).into()))
        .unwrap();
    assert!(server.receive().is_err());
}
#[test]
fn oversized_and_unmasked_frames_rejected() {
    for masked in [true, false] {
        let (client, owner) = peer("/ws/cameras/cam-1/live", &["test-key"]);
        let mut client = client.unwrap();
        let mut server = owner.join().unwrap().unwrap();
        if masked {
            client
                .send(Message::Binary(vec![0; MAX_JPEG + 49].into()))
                .unwrap();
        } else {
            client.get_mut().write_all(&[0x82, 1, 0]).unwrap();
        }
        assert!(server.receive().is_err());
    }
}
#[test]
fn handshake_byte_and_absolute_deadline_budgets() {
    for oversized in [true, false] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut peer = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let socket = listener.accept().unwrap().0;
        let mut stream = BoundedStream {
            socket,
            deadline: Some(Instant::now() + Duration::from_millis(30)),
            remaining: Some(8),
            write_failed: false,
            quickack: false,
            quickack_requests: 0,
        };
        let start = Instant::now();
        if oversized {
            peer.write_all(b"0123456789").unwrap();
            stream.read_exact(&mut [0; 8]).unwrap();
            assert!(stream.read(&mut [0; 1]).is_err());
        } else {
            peer.write_all(b"x").unwrap();
            stream.read_exact(&mut [0; 1]).unwrap();
            assert!(stream.read(&mut [0; 1]).is_err());
            assert!(start.elapsed() < Duration::from_secs(1));
        }
    }
    for _ in 0..16 {
        let nonce = nonce().unwrap();
        assert_eq!(nonce.len(), 16);
        assert!(nonce.bytes().all(|b| b.is_ascii_hexdigit()));
        assert!((1..=(1 << 40)).contains(&initial_generation().unwrap()));
    }
}

#[test]
fn partial_frame_survives_wakeup_and_fragmented_message_has_total_cap() {
    use tungstenite::protocol::frame::{
        Frame,
        coding::{Data, OpCode},
    };
    let (client, owner) = peer("/ws/cameras/cam-1/live", &["test-key"]);
    let mut client = client.unwrap();
    let mut server = owner.join().unwrap().unwrap();
    let frame = [
        0x81,
        0x84,
        1,
        2,
        3,
        4,
        b'p' ^ 1,
        b'o' ^ 2,
        b'n' ^ 3,
        b'g' ^ 4,
    ];
    client.get_mut().write_all(&frame[..3]).unwrap();
    assert!(server.receive().unwrap().is_none());
    client.get_mut().write_all(&frame[3..]).unwrap();
    assert!(matches!(server.receive().unwrap(), Some(Message::Text(t)) if t == "pong"));
    client
        .send(Message::Frame(Frame::message(
            vec![0; MAX_JPEG / 2],
            OpCode::Data(Data::Binary),
            false,
        )))
        .unwrap();
    assert!(server.receive().unwrap().is_none());
    client
        .send(Message::Frame(Frame::message(
            vec![0; MAX_JPEG / 2 + 49],
            OpCode::Data(Data::Continue),
            true,
        )))
        .unwrap();
    assert!(server.receive().is_err());
}

#[test]
fn empty_fragment_drip_cannot_starve_control_deadline() {
    use tungstenite::protocol::frame::{
        Frame,
        coding::{Data, OpCode},
    };
    let (client, owner) = peer("/ws/cameras/cam-1/live", &["test-key"]);
    let mut client = client.unwrap();
    let mut server = owner.join().unwrap().unwrap();
    client
        .send(Message::Frame(Frame::message(
            vec![],
            OpCode::Data(Data::Binary),
            false,
        )))
        .unwrap();
    let drip = thread::spawn(move || {
        for _ in 0..50 {
            client
                .send(Message::Frame(Frame::message(
                    vec![],
                    OpCode::Data(Data::Continue),
                    false,
                )))
                .unwrap();
            thread::sleep(Duration::from_millis(5));
        }
        client
    });
    let start = Instant::now();
    assert!(server.receive().unwrap().is_none());
    assert!(start.elapsed() < Duration::from_millis(200));
    let _client = drip.join().unwrap();
}

#[test]
fn write_deadline_failure_is_fatal_instead_of_an_idle_read() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let _peer = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    let mut stream = BoundedStream {
        socket: listener.accept().unwrap().0,
        deadline: Some(Instant::now()),
        remaining: None,
        write_failed: false,
        quickack: false,
        quickack_requests: 0,
    };
    assert_eq!(
        stream.write(b"pong").unwrap_err().kind(),
        io::ErrorKind::TimedOut
    );
    assert!(stream.write_failed);
    let mut connection = Connection(WebSocket::from_raw_socket(
        stream,
        tungstenite::protocol::Role::Server,
        None,
    ));
    // Tungstenite can recover from an automatic Pong WouldBlock and return a
    // buffered text message. Inject that successful lower-level read outcome.
    assert!(
        connection
            .finish_receive(Ok(Message::Text("must not publish".into())))
            .is_err()
    );
    assert!(connection.receive().is_err());
}

#[cfg(target_os = "linux")]
#[test]
fn quickack_is_reapplied_after_positive_reads_and_keeps_the_borrowed_socket_open() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let owner = thread::spawn(move || {
        Connection::accept_with_quickack(
            listener.accept().unwrap().0,
            &Auth::new("cam", b"test-key").unwrap(),
            true,
        )
        .unwrap()
    });
    let mut request = format!("ws://{address}/ws/cameras/cam/live")
        .into_client_request()
        .unwrap();
    request
        .headers_mut()
        .insert("X-API-Key", "test-key".parse().unwrap());
    let mut client = tungstenite::client(request, TcpStream::connect(address).unwrap())
        .unwrap()
        .0;
    let mut server = owner.join().unwrap();
    assert!(server.quickack_requested());
    assert!(server.quickack_requests() > 0); // Handshake reads are covered too.
    for text in ["one", "two"] {
        let before = server.quickack_requests();
        client.send(Message::Text(text.into())).unwrap();
        assert!(matches!(server.receive().unwrap(), Some(Message::Text(t)) if t==text));
        assert!(server.quickack_requests() > before);
        server.send_text("ack").unwrap();
        assert_eq!(client.read().unwrap().into_text().unwrap(), "ack");
    }
}

#[cfg(not(target_os = "linux"))]
#[test]
fn unsupported_quickack_is_rejected_before_handshake() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let _peer = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    assert!(
        Connection::accept_with_quickack(
            listener.accept().unwrap().0,
            &Auth::new("cam", b"key").unwrap(),
            true
        )
        .is_err()
    );
}
