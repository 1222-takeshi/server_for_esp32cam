import json

import pytest

from scripts.spin_on_run import interval_classify as ic

LOW = 1_000_000  # published_k of the interval under test
HIGH = 1_300_000  # published_{k+1}: D = 300 ms


def frame(sequence, **fields):
    """A frame record of the D7 schema; every field defaults to null."""
    base = {
        "sequence": sequence,
        "boot_id": 7,
        "generation": 1,
        "first_byte_us": None,
        "last_byte_us": None,
        "bytes": 20_000,
        "received_us": None,
        "captured_us": None,
        "sent_us": None,
        "begun_us": None,
        "finished_us": None,
        "published_us": None,
        "compute_us": None,
        "stage": "published",
        "reads": 10,
        "max_read_us": 1_000,
        "read_timeouts": 0,
        "tcp": None,
    }
    return {**base, **fields}


def pair(**second):
    """Published frames 10 -> 11 around the interval; `second` overrides frame 11."""
    return [
        frame(10, published_us=LOW, received_us=LOW - 50_000, last_byte_us=LOW - 50_000),
        frame(11, published_us=HIGH, **second),
    ]


def only(result):
    [interval] = result["intervals"]
    return interval


# --- main classes -----------------------------------------------------------------------


@pytest.mark.parametrize(("span", "arrival"), [(200_000, False), (200_001, True)])
def test_a_long_receive_span_is_arrival(span, arrival):
    frames = pair(first_byte_us=1_050_000, last_byte_us=1_050_000 + span)
    interval = only(ic.classify(frames))
    assert interval["flags"]["arrival"] is arrival
    assert interval["primary"] == ("arrival" if arrival else "other")


@pytest.mark.parametrize(("gap", "arrival"), [(300_000, False), (300_001, True)])
def test_a_wide_first_byte_gap_between_adjacent_frames_is_arrival(gap, arrival):
    start = 950_000
    frames = [
        frame(10, published_us=LOW, first_byte_us=start, last_byte_us=start + 20_000),
        frame(
            11,
            published_us=HIGH,
            first_byte_us=start + gap,
            last_byte_us=start + gap + 20_000,
        ),
    ]
    # frame 11 finished receiving inside the interval, 300 ms after its neighbour's first byte
    interval = only(ic.classify(frames))
    assert interval["flags"]["arrival"] is arrival


@pytest.mark.parametrize(("compute", "inference"), [(199_999, False), (200_000, True)])
def test_a_long_compute_is_inference(compute, inference):
    frames = pair(compute_us=compute, finished_us=HIGH - 1, begun_us=HIGH - compute - 1)
    interval = only(ic.classify(frames))
    assert interval["flags"]["inference"] is inference
    assert interval["primary"] == ("inference" if inference else "other")


@pytest.mark.parametrize(("wait", "inference"), [(99_999, False), (100_000, True)])
def test_a_long_wait_before_the_worker_began_is_inference(wait, inference):
    frames = pair(received_us=1_050_000, begun_us=1_050_000 + wait, finished_us=HIGH - 1)
    interval = only(ic.classify(frames))
    assert interval["flags"]["inference"] is inference


def test_an_interval_with_no_cause_is_other():
    frames = pair(
        first_byte_us=1_100_000,
        last_byte_us=1_120_000,
        received_us=1_120_000,
        begun_us=1_130_000,
        finished_us=1_290_000,
        compute_us=160_000,
    )
    interval = only(ic.classify(frames))
    assert interval["primary"] == "other" and interval["reasons"] == []
    assert interval["flags"] == {
        "arrival": False,
        "inference": False,
        "camera": False,
        "cpu_contention": None,
    }


def test_several_causes_keep_every_flag_and_the_first_class_in_order():
    frames = pair(
        first_byte_us=1_000_000,
        last_byte_us=1_250_000,  # arrival: span 250 ms
        received_us=1_250_000,
        begun_us=1_250_000,
        finished_us=1_290_000,
        compute_us=250_000,  # inference too
    )
    result = ic.classify(frames)
    interval = only(result)
    assert interval["primary"] == "arrival"
    assert interval["flags"]["arrival"] and interval["flags"]["inference"]
    assert result["flag_counts"]["arrival_and_inference"] == 1
    assert result["by_primary"] == {"arrival": 1, "inference": 0, "other": 0}


def test_only_inference_when_the_arrival_side_is_clean():
    frames = pair(
        first_byte_us=1_100_000,
        last_byte_us=1_120_000,
        received_us=1_120_000,
        begun_us=1_130_000,
        finished_us=1_290_000,
        compute_us=210_000,
    )
    assert only(ic.classify(frames))["primary"] == "inference"


# --- the camera flag is separate -------------------------------------------------------------


@pytest.mark.parametrize(("gap", "camera"), [(250_000, False), (250_001, True)])
def test_the_camera_flag_does_not_change_the_main_class(gap, camera):
    frames = [
        frame(10, published_us=LOW, captured_us=700_000, received_us=LOW - 1),
        frame(11, published_us=HIGH, captured_us=700_000 + gap, received_us=HIGH - 10_000),
    ]
    interval = only(ic.classify(frames))
    assert interval["flags"]["camera"] is camera
    assert interval["primary"] == "other"
    if camera:
        assert any(reason.startswith("capture_gap:11") for reason in interval["reasons"])


# --- scope of the evidence -----------------------------------------------------------------------


def test_frames_outside_the_interval_do_not_count():
    frames = pair() + [
        frame(12, first_byte_us=2_000_000, last_byte_us=2_900_000, received_us=2_900_000),
        frame(8, first_byte_us=100_000, last_byte_us=900_000, received_us=900_000),
        frame(13, compute_us=900_000, finished_us=5_000_000, published_us=None),
    ]
    assert only(ic.classify(frames))["primary"] == "other"


def test_a_frame_that_was_never_published_still_explains_the_wait():
    frames = pair() + [
        frame(
            12,
            stage="stale",
            first_byte_us=1_000_000,
            last_byte_us=1_240_000,
            received_us=1_240_000,
        )
    ]
    assert only(ic.classify(frames))["primary"] == "arrival"


def test_only_stretched_intervals_are_listed_and_counted():
    frames = [
        frame(1, published_us=0),
        frame(2, published_us=250_000),  # exactly 250 ms: not stretched
        frame(3, published_us=500_001),  # 250,001: stretched
        frame(4, published_us=700_000),
    ]
    result = ic.classify(frames)
    assert result["intervals_gt250"] == 1 and only(result)["d_us"] == 250_001
    assert ic.classify([])["intervals_gt250"] == 0 and ic.classify(frames[:1])["intervals"] == []


def test_a_hold_onset_gap_is_marked_and_a_generation_change_alone_is_not():
    frames = [
        frame(1, published_us=0),
        frame(2, published_us=300_000, generation=2),
        frame(3, published_us=900_000, generation=3),
    ]
    first, second = ic.classify(frames)["intervals"]
    assert first["cross_generation"] and not first["hold_onset"]
    assert second["cross_generation"] and second["hold_onset"]


def test_tcp_snapshots_in_the_interval_are_summarised():
    snap = {"total_retrans": 5, "retrans": 0, "lost": 1, "rto_us": 400_000, "rcv_rtt_us": 9_000}
    frames = pair(received_us=1_100_000, tcp=snap) + [
        frame(12, received_us=1_000_500, tcp={**snap, "total_retrans": 3, "lost": 4})
    ]
    tcp = only(ic.classify(frames))["tcp"]
    assert tcp["total_retrans_delta"] == 2 and tcp["max_lost"] == 4 and tcp["max_rto_us"] == 400_000
    assert only(ic.classify(pair()))["tcp"] is None


# --- R2 --------------------------------------------------------------------------------------------


def r2_tick(native_us, wait_ns, tid="1", **extra):
    return {
        "kind": "tick",
        "pi_uptime_s": native_us / 1_000_000,
        "sched": {tid: [1, wait_ns, 1]},
        "snmp": None,
        "netstat": None,
        "ss": None,
        "freq_khz": 1_500_000,
        "throttled": 0,
        **extra,
    }


def test_cpu_contention_is_the_summed_run_queue_wait_over_the_interval():
    ticks = [r2_tick(900_000, 1_000_000), r2_tick(1_400_000, 1_000_000 + 50_000_000)]
    over = only(ic.classify(pair(), ic.R2Index(ticks, 0.0)))
    assert over["flags"]["cpu_contention"] is True and over["r2"]["sched_wait_ns"] == 50_000_000
    under = [r2_tick(900_000, 1_000_000), r2_tick(1_400_000, 1_000_000 + 49_999_999)]
    assert only(ic.classify(pair(), ic.R2Index(under, 0.0)))["flags"]["cpu_contention"] is False
    assert over["primary"] == "other"  # the flag never becomes a main class
    # a thread that appears during the interval counts in full; threads add up
    ticks = [
        {**r2_tick(900_000, 0), "sched": {"1": [1, 10_000_000, 1]}},
        {**r2_tick(1_400_000, 0), "sched": {"1": [1, 40_000_000, 1], "2": [1, 25_000_000, 1]}},
    ]
    sums = only(ic.classify(pair(), ic.R2Index(ticks, 0.0)))["r2"]["sched_wait_ns"]
    assert sums == 30_000_000 + 25_000_000


def test_r2_context_reports_the_network_and_clock_facts():
    first = r2_tick(
        900_000,
        0,
        snmp={"RetransSegs": 10},
        netstat={"TCPOFOQueue": 100},
        ss=[{"retrans_total": 4, "lastrcv_ms": 20}],
    )
    last = r2_tick(
        1_400_000,
        0,
        snmp={"RetransSegs": 13},
        netstat={"TCPOFOQueue": 105},
        ss=[{"retrans_total": 6, "lastrcv_ms": 280}],
        freq_khz=600_000,
        throttled=0x50005,
    )
    context = only(ic.classify(pair(), ic.R2Index([first, last], 0.0)))["r2"]
    assert context["tcp_retrans_segs_delta"] == 3 and context["tcp_ofo_queue_delta"] == 5
    assert context["ss_retrans_total_delta"] == 2 and context["ss_lastrcv_ms_max"] == 280
    assert context["freq_khz_min"] == 600_000 and context["throttled_any"] is True


def test_r2_that_does_not_cover_the_interval_leaves_the_flag_unjudged():
    far = [r2_tick(100_000, 0), r2_tick(200_000, 99_000_000)]
    assert only(ic.classify(pair(), ic.R2Index(far, 0.0)))["flags"]["cpu_contention"] is None
    late = [r2_tick(1_100_000, 0), r2_tick(1_150_000, 99_000_000)]  # nothing at or before LOW
    assert only(ic.classify(pair(), ic.R2Index(late, 0.0)))["flags"]["cpu_contention"] is None
    unaligned = ic.R2Index([r2_tick(900_000, 0), r2_tick(1_400_000, 9_000_000_000)], None)
    assert not unaligned.available()
    assert only(ic.classify(pair(), unaligned))["flags"]["cpu_contention"] is None


def test_the_clock_offset_places_the_uptime_on_the_native_clock():
    mono, raw = 10_000_000_000, 50_000_000_000
    report = {
        "host_clock_epoch_ns": raw + 100 - 2_000,
        "host_clock_witness": {"begin": [mono, mono + 200, raw, raw + 200]},
    }
    offset = ic.clock_offset_us(report)
    assert (mono + 100) / 1000 + offset == pytest.approx(2.0)
    for broken in (None, {}, {"host_clock_epoch_ns": 1}, {"host_clock_witness": {"begin": [1]}}):
        assert ic.clock_offset_us(broken) is None


# --- evidence directory ---------------------------------------------------------------------------


def write_lines(path, records, junk=0):
    lines = [json.dumps(r) for r in records] + ["{broken"] * junk
    path.write_text("\n".join(lines) + "\n")


HEADER = {
    "v": 1,
    "clock": "native_monotonic_us",
    "nominal_interval_us": 200_000,
    "capacity": 100,
    "records": 2,
    "lost_records": 3,
}


def test_evaluate_reads_frames_without_r2(tmp_path):
    write_lines(tmp_path / "frames.jsonl", [HEADER, *pair()], junk=2)
    result = ic.evaluate(tmp_path)
    assert result["classifiable"] is True and result["report_only"] is True
    assert result["frames"]["records"] == 2 and result["frames"]["lost_records"] == 3
    assert result["frames"]["unreadable_lines"] == 2
    assert result["r2"] == {"present": False, "aligned": False, "note": "r2_missing"}
    assert result["intervals_gt250"] == 1 and result["by_primary"]["other"] == 1
    assert only(result)["flags"]["cpu_contention"] is None


def test_evaluate_uses_r2_when_the_native_report_aligns_the_clocks(tmp_path):
    mono, raw = 10_000_000_000, 50_000_000_000
    native = {
        "host_clock_epoch_ns": raw,
        "host_clock_witness": {"begin": [mono, mono, raw, raw]},
    }
    (tmp_path / "native.json").write_text(json.dumps(native))
    write_lines(tmp_path / "frames.jsonl", [HEADER, *pair()])
    # raw == epoch at the witness, so native_us = uptime_us - mono_us
    ticks = [
        {**r2_tick(0, 0), "pi_uptime_s": (900_000 + mono / 1000) / 1_000_000},
        {**r2_tick(0, 80_000_000), "pi_uptime_s": (1_400_000 + mono / 1000) / 1_000_000},
    ]
    write_lines(tmp_path / "r2-sampler.jsonl", [{"kind": "header"}, *ticks])
    result = ic.evaluate(tmp_path)
    assert result["r2"]["aligned"] is True
    assert only(result)["flags"]["cpu_contention"] is True
    assert result["flag_counts"]["cpu_contention"] == 1


def test_r2_without_a_clock_witness_is_present_but_not_judged(tmp_path):
    write_lines(tmp_path / "frames.jsonl", [HEADER, *pair()])
    ticks = [r2_tick(900_000, 0), r2_tick(1_400_000, 9_000_000_000)]
    write_lines(tmp_path / "r2-sampler.jsonl", ticks)
    result = ic.evaluate(tmp_path)
    assert result["r2"]["present"] and not result["r2"]["aligned"]
    assert only(result)["flags"]["cpu_contention"] is None
    assert result["flag_counts"]["cpu_contention_not_judged"] == 1


def test_without_frames_the_run_is_reported_as_unclassifiable(tmp_path):
    result = ic.evaluate(tmp_path)
    assert result["classifiable"] is False and result["reason"] == "frames_jsonl_missing"
    assert result["intervals"] == [] and result["by_primary"] is None
    assert result["intervals_gt250"] is None  # no native.json either
    samples = [
        {"boot_id": 1, "generation": 1, "sequence": n, "published_us": t}
        for n, t in enumerate([1_000, 201_000, 501_000, 701_000, 1_001_000])
    ]
    native = {"measurement": {"start_us": 0, "end_us": 10**7}, "samples": samples}
    (tmp_path / "native.json").write_text(json.dumps(native))
    counted = ic.evaluate(tmp_path)
    assert counted["classifiable"] is False and counted["intervals_gt250"] == 2


def test_a_frames_file_without_a_header_is_still_read(tmp_path):
    write_lines(tmp_path / "frames.jsonl", pair())
    result = ic.evaluate(tmp_path)
    assert result["classifiable"] and result["frames"]["header"] is None
    assert result["frames"]["records"] == 2 and result["intervals_gt250"] == 1


def test_main_writes_the_report_once(tmp_path, capsys):
    write_lines(tmp_path / "frames.jsonl", [HEADER, *pair()])
    assert ic.main([str(tmp_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["classifiable"] is True and printed["by_primary"]["other"] == 1
    written = json.loads((tmp_path / "interval-classification.json").read_text())
    assert written["report_only"] is True and written["v"] == 1
    assert ic.main([str(tmp_path)]) == 2  # never overwrites
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ic.main([str(empty)]) == 0
    assert json.loads((empty / "interval-classification.json").read_text())["classifiable"] is False
