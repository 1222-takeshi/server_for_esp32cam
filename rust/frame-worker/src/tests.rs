use super::*;
use std::io::Cursor;

fn wire(profile: bool, boot: u64, sequence: u64) -> Vec<u8> {
    let mut bytes = if profile {
        b"ECF2".to_vec()
    } else {
        b"ECF1".to_vec()
    };
    for value in [boot, sequence, sequence, sequence + 1] {
        bytes.extend(value.to_be_bytes());
    }
    if profile {
        bytes.extend(MAX_GENERATION.to_be_bytes());
    }
    bytes.extend(2u32.to_be_bytes());
    bytes.extend([0xff, 0xd8]); // Envelope decoding does not validate JPEG contents.
    bytes
}

#[test]
fn versions_match_python_network_byte_order() {
    for profile in [false, true] {
        let frame = decode(&wire(profile, u64::MAX, 42), true).unwrap();
        assert_eq!(
            (frame.boot, frame.sequence, frame.captured_us, frame.sent_us),
            (u64::MAX, 42, 42, 43)
        );
        assert_eq!(frame.generation, profile.then_some(MAX_GENERATION));
        assert_eq!(frame.jpeg, [0xff, 0xd8]);
    }
    assert!(decode(&wire(true, 1, 1), false).is_err());
}

#[test]
fn malformed_identity_time_generation_and_length_are_rejected() {
    let valid = wire(true, 1, 1);
    for offset in [4, 12, 20, 36] {
        let mut bytes = valid.clone();
        bytes[offset..offset + 8].fill(0);
        assert!(decode(&bytes, true).is_err());
    }
    for (offset, value) in [(28, 0u64), (36, MAX_GENERATION + 1)] {
        let mut bytes = valid.clone();
        bytes[offset..offset + 8].copy_from_slice(&value.to_be_bytes());
        assert!(decode(&bytes, true).is_err());
    }
    for length in 0..valid.len() {
        assert!(decode(&valid[..length], true).is_err());
    }
    let mut bytes = valid.clone();
    bytes.push(0);
    assert!(decode(&bytes, true).is_err());
    bytes[0] = 0;
    assert!(decode(&bytes, true).is_err());
}

#[test]
fn payload_boundaries_and_early_length_rejection() {
    for length in [1, MAX_JPEG] {
        let mut bytes = wire(false, 1, 1);
        bytes.truncate(36);
        bytes.extend((length as u32).to_be_bytes());
        bytes.resize(40 + length, 0);
        assert_eq!(
            read_frame(&mut Cursor::new(bytes), false)
                .unwrap()
                .unwrap()
                .jpeg
                .len(),
            length
        );
    }
    for length in [0, MAX_JPEG as u32 + 1, u32::MAX] {
        let mut bytes = wire(false, 1, 1);
        bytes.truncate(36);
        bytes.extend(length.to_be_bytes());
        assert_eq!(
            read_frame(&mut Cursor::new(bytes), false)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
    }
}

#[test]
fn stream_eof_truncation_and_multiple_frames() {
    let bytes = wire(true, 1, 1);
    for length in 1..bytes.len() {
        assert!(read_frame(&mut Cursor::new(&bytes[..length]), true).is_err());
    }
    let mut stream = Cursor::new([bytes.clone(), bytes].concat());
    assert!(read_frame(&mut stream, true).unwrap().is_some());
    assert!(read_frame(&mut stream, true).unwrap().is_some());
    assert!(read_frame(&mut stream, true).unwrap().is_none());
}

#[test]
fn ordering_rejects_without_mutation_and_bounds_retired_boots() {
    let mut order = FrameOrder::default();
    let frame = |boot, seq| decode(&wire(true, boot, seq), true).unwrap();
    assert_eq!(order.observe(&frame(1, 2)), Ok(("new", 0)));
    assert_eq!(order.observe(&frame(1, 2)), Err("duplicate"));
    assert_eq!(order.observe(&frame(1, 1)), Err("stale"));
    assert_eq!(order.observe(&frame(1, 5)), Ok(("new", 2)));
    let mut stale = frame(1, 6);
    stale.captured_us = 5;
    assert_eq!(order.observe(&stale), Err("stale"));
    assert_eq!(order.observe(&frame(2, 1)), Ok(("boot_changed", 0)));
    assert_eq!(order.observe(&frame(1, 99)), Err("retired_boot"));
    for boot in 3..100 {
        assert_eq!(order.observe(&frame(boot, 1)), Ok(("boot_changed", 0)));
    }
    assert_eq!(order.retired.len(), 32);
}

#[test]
fn latest_slot_overwrites_one_item_and_never_waits_on_contention() {
    let slot = LatestSlot::default();
    assert_eq!(slot.replace(1), Ok(None));
    assert_eq!(slot.replace(2), Ok(Some(1)));
    let held = slot.0.lock().unwrap();
    assert_eq!(slot.replace(3), Err(3));
    assert_eq!(slot.take(), None);
    drop(held);
    assert_eq!(slot.take(), Some(2));
    assert_eq!(slot.take(), None);
}

#[test]
fn latest_frame_slot_needs_no_default_frame() {
    let slot = LatestSlot::<Frame>::default();
    let frame = decode(&wire(true, 1, 1), true).unwrap();
    assert_eq!(slot.replace(frame), Ok(None));
    assert_eq!(slot.take().unwrap().sequence, 1);
}
