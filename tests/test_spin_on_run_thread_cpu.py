from fractions import Fraction

from scripts.spin_on_run import thread_cpu as tc

CLK = 100


class FakeProc:
    """Three busy threads whose on-cpu time follows a simple model."""

    def __init__(self, *, schedstat=True, drop_tid=None, sched_scale=1.0):
        self.now = 0.0
        self.schedstat = schedstat
        self.drop_tid = drop_tid
        self.scale = sched_scale

    def tids(self, pid):
        return [1, 2, 3]

    def self_ticks(self, pid):
        return int(self.now * 100 * 0.005)  # 0.5 % of one core

    def read(self, pid, tid, name):
        if tid == self.drop_tid and self.now > 0.5:
            raise FileNotFoundError
        ticks = int(self.now * CLK)  # busy: one tick per 10 ms
        if name == "stat":
            return stat_line(ticks, 0, tid)
        if name == "schedstat":
            if not self.schedstat:
                raise PermissionError
            return f"{int(ticks * 10_000_000 * self.scale)} 5 7"
        raise AssertionError(name)


def stat_line(utime, stime, start):
    """Fields after the command name: state, 10 fillers, utime, stime, 6 fillers, starttime."""
    after = ["S", *["0"] * 10, str(utime), str(stime), *["0"] * 6, str(start), "0"]
    return "5 (a b) " + " ".join(after)


def test_stat_and_schedstat_parsing():
    assert tc.parse_stat(stat_line(7, 3, 99)) == (10, 99)
    assert tc.parse_schedstat("123 4 5\n") == 123


def collect(proc, duration=3.0):
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds
        proc.now = clock["t"]

    samples = tc.run_sampler(proc, 1, now, sleep, lambda: clock["t"] > duration, t0=0.0)
    return samples


def test_expected_samples_counts_slots_in_the_half_open_window():
    assert tc.expected_samples(0.0, (1.0, 2.0)) == 10
    assert tc.expected_samples(0.0, (1.05, 2.0)) == 9
    assert tc.expected_samples(0.0, (2.0, 2.0)) == 0


def test_a_healthy_run_meets_every_sampler_criterion():
    samples = collect(FakeProc())
    got = tc.evaluate_run(samples, (0.5, 2.5), 0.0, CLK)
    assert got["taken"] >= 0.95 * got["expected"] and got["expected"] == 20
    assert got["m1"] and got["m2"] and got["m3"] and got["m4"] and got["m5_sampler"]
    assert got["m1_moving_keys"] == 3


def test_unreadable_schedstat_is_not_measurable_but_partial_is_a_failure():
    none = tc.evaluate_run(collect(FakeProc(schedstat=False)), (0.5, 2.5), 0.0, CLK)
    assert not none["schedstat_readable"] and not none["m1"]
    infer = {"m5_infer": True}
    runs = [none, none, none]
    assert (
        tc.outcome(harness_failure=False, runs=runs, infer=infer, m6=True, m7=True)["id"] == "M-D2"
    )
    good = tc.evaluate_run(collect(FakeProc()), (0.5, 2.5), 0.0, CLK)
    mixed = [none, good, good]
    assert (
        tc.outcome(harness_failure=False, runs=mixed, infer=infer, m6=True, m7=True)["id"] == "M-D3"
    )


def test_exited_threads_leave_the_denominator_but_do_not_break_m1():
    got = tc.evaluate_run(collect(FakeProc(drop_tid=3)), (0.5, 2.5), 0.0, CLK)
    assert got["m1_ratio"] == 1 and got["m1_moving_keys"] == 2 and not got["m1"]  # needs 3 keys


def test_schedstat_and_stat_must_agree_within_five_percent_plus_two_ticks():
    assert tc.evaluate_run(collect(FakeProc(sched_scale=1.04)), (0.5, 2.5), 0.0, CLK)["m4"]
    assert not tc.evaluate_run(collect(FakeProc(sched_scale=1.2)), (0.5, 2.5), 0.0, CLK)["m4"]


def test_late_slots_are_skipped_without_catching_up():
    clock = {"t": 0.0}
    proc = FakeProc()
    calls = {"n": 0}

    def now():
        calls["n"] += 1
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds + (0.25 if clock["t"] > 1.0 and clock["t"] < 1.2 else 0)
        proc.now = clock["t"]

    samples = tc.run_sampler(proc, 1, now, sleep, lambda: clock["t"] > 3.0, t0=0.0)
    slots = [s["slot"] for s in samples]
    assert len(slots) == len(set(slots)) and max(s["lateness"] for s in samples) <= 0.1


def test_sparse_sampling_fails_m2():
    samples = collect(FakeProc())[::3]
    assert not tc.evaluate_run(samples, (0.5, 2.5), 0.0, CLK)["m2"]


def test_worker_impact_thresholds_are_exact():
    x = [100_000] * 240
    ok = tc.combine_infer(x, [102_000] * 240)
    assert ok["m5_infer"] and ok["mean_ratio"] == Fraction(102, 100)
    assert not tc.combine_infer(x, [102_001] * 240)["m5_infer"]
    y = [100_000] * 228 + [105_000] * 12  # rank 228 is the last of the lower group
    assert tc.combine_infer(x, y)["p95_ratio"] == 1
    y = [100_000] * 227 + [105_001] * 13
    assert not tc.combine_infer(x, y)["m5_infer"]
    assert not tc.combine_infer(x[:10], x)["m5_infer"]


def test_decision_table_order():
    good = tc.evaluate_run(collect(FakeProc()), (0.5, 2.5), 0.0, CLK)
    runs, infer = [good] * 3, {"m5_infer": True}
    f = lambda **kw: tc.outcome(  # noqa: E731
        **{"harness_failure": False, "runs": runs, "infer": infer, "m6": True, "m7": True, **kw}
    )["id"]
    assert f() == "M-D4"
    assert f(harness_failure=True) == "M-D1" and f(m6=False) == "M-D1" and f(m7=False) == "M-D1"
    assert f(infer={"m5_infer": False}) == "M-D3"
    assert f(runs=runs[:2]) == "M-D3"
    assert tc.M_ORDER == "XYYXXY" and tc.M_ORDER.count("Y") == 3


def test_m_environment_passes_nothing_to_the_unmodified_worker():
    env = tc.m_env("Y")
    assert env == {"PATH": "/usr/bin:/bin"}  # no spinning switch, no gap setting
    assert tc.m_env("X") == env
    assert tc.FRAME_GAP_S == 0.04


def test_the_gap_check_needs_eighty_gaps_of_forty_milliseconds_on_top_of_the_work():
    def rows(span, busy=0):
        work = [{"wall_us": 0, "total_us": busy // 80}] * 85
        work[4] = {"wall_us": 1_000, "total_us": 1}
        work[84] = {"wall_us": 1_000 + span, "total_us": busy // 80}
        return work

    assert tc.gap_present(rows(3_200_000))
    assert not tc.gap_present(rows(3_199_999))
    # Processing alone fills 4 s: the literal bound holds without a single gap.
    assert not tc.gap_present(rows(4_000_000, busy=4_000_000))
    assert tc.gap_present(rows(7_200_000, busy=4_000_000))
    assert not tc.gap_present([])


def test_m_runs_the_registered_worker_not_the_new_binary():
    from scripts.spin_on_run import cli, common, start_gate

    assert common.HASHES["pr616_worker"].startswith("91a7e709")
    source = __import__("inspect").getsource(cli._offline_config)
    assert 'cfg["pr616_worker"] if thread else cfg["worker"]' in source
    assert "thread: bool = False" in __import__("inspect").getsource(start_gate.evaluate)


def test_sampler_cpu_comes_from_its_own_process_not_the_worker(tmp_path):
    (tmp_path / "self").mkdir()
    (tmp_path / "4242").mkdir()
    (tmp_path / "self" / "stat").write_text(stat_line(30, 12, 5))
    (tmp_path / "4242" / "stat").write_text(stat_line(900, 900, 5))
    assert tc.ProcReader(str(tmp_path)).self_ticks(4242) == 42
