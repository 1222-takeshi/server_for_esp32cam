import json
import re
import stat
import textwrap

import pytest

from scripts.spin_on_run import cli, r2_sampler

SCRIPT = r2_sampler.REMOTE_SCRIPT

# --- static allowlist of the remote script (read-only guarantee) -------------------------

READ_ONLY_COMMANDS = {
    "cat", "read", "ss", "iw", "vcgencmd", "pgrep", "sleep", "date", "awk", "echo", "printf",
    "getconf", "command", "[", "return", "continue", "for", "fi", "done", "true",
}  # fmt: skip
LEADING_KEYWORDS = {"if", "then", "else", "elif", "while", "do", "!"}
ALLOWED_REDIRECTS = (r"2>/dev/null", r">/dev/null", r"2>&1")


def sanitize(script: str) -> str:
    """Strip comments, quoted text, parameter and arithmetic expansions and read redirections."""
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    text = "\n".join(lines)
    text = re.sub(r'"[^"]*"|\'[^\']*\'', '""', text)
    text = re.sub(r"\$\(\([^)]*\)\)", "0", text)
    text = re.sub(r"\$\{[^}]*\}", "x", text)
    for allowed in ALLOWED_REDIRECTS:
        text = re.sub(r"\s*" + allowed, "", text)
    return re.sub(r"<\s*[/\w$.-]+", "", text)  # input redirection only reads


def violations(script: str) -> list[str]:
    """Everything in a script that is not a plain read: commands, writes, expansions."""
    text = sanitize(script)
    found = []
    if ">" in text:
        found.append("redirect-out")
    if "<<" in script or "`" in text:
        found.append("heredoc-or-backtick")
    defined = set(re.findall(r"^\s*(\w+)\(\)\s*\{", text, flags=re.M))
    text = text.replace("$(", "\n")
    for segment in re.split(r"[;|&\n{}()]", text):
        words = segment.split()
        while words and words[0] in LEADING_KEYWORDS:
            words = words[1:]
        while words and re.fullmatch(r"\w+=\S*", words[0]):
            words = words[1:]
        if words and words[0] not in READ_ONLY_COMMANDS | defined:
            found.append(f"command:{words[0]}")
    return found


def test_the_remote_script_only_reads():
    assert violations(SCRIPT.read_text()) == []


@pytest.mark.parametrize(
    "bad",
    [
        "rm -f /tmp/x",
        "echo x > /tmp/x",
        "echo x >> /tmp/x",
        "ss -tin 2>/tmp/err",
        "cat /proc/stat | tee /tmp/x",
        "sudo cat /proc/stat",
        "kill -9 1",
        "pkill frame-live",
        "systemctl stop esp32cam-server",
        "cp /proc/stat /tmp/s",
        "mv a b",
        "x=$(rm a)",
        "echo `rm a`",
        "cat <<EOF",
        "chmod 600 a",
        'eval "$cmd"',
        "sed -i s/a/b/ f",
        "if [ -r a ]; then touch a; fi",
    ],
)
def test_the_checker_catches_writes_and_other_commands(bad):
    assert violations(bad), bad


def test_the_checker_names_an_added_command():
    commands = {v.split(":", 1)[1] for v in violations(SCRIPT.read_text() + "\nrm x\n")}
    assert commands == {"rm"}


# --- argument handling -------------------------------------------------------------------


def test_ssh_argv_passes_the_script_through_stdin_with_validated_arguments():
    argv = r2_sampler.ssh_argv("pi@raspi.local", "192.168.1.50", 660, options=["-i", "/keys/id"])
    assert argv[0] == "ssh" and "BatchMode=yes" in argv
    assert argv[-7:] == ["sh", "-s", "--", "192.168.1.50", "660", "frame-live", "wlan0"]
    assert argv.index("-i") < argv.index("pi@raspi.local") < argv.index("sh")
    assert not any("sudo" in part for part in argv)


@pytest.mark.parametrize(
    "bad",
    [
        {"host": "-oProxyCommand=x"},
        {"host": "pi;rm"},
        {"host": ""},
        {"camera_ip": "192.168.1.256"},
        {"camera_ip": "192.168.1.5; rm x"},
        {"camera_ip": "cam.local"},
        {"seconds": 0},
        {"seconds": 4501},
        {"process": "a b"},
        {"interface": "wlan0;x"},
    ],
)
def test_ssh_argv_rejects_values_that_would_reach_the_remote_shell(bad):
    args = {"host": "pi", "camera_ip": "192.168.1.50", "seconds": 60, **bad}
    with pytest.raises(ValueError):
        r2_sampler.ssh_argv(args.pop("host"), args.pop("camera_ip"), args.pop("seconds"), **args)


# --- parsers -------------------------------------------------------------------------------

SS = [
    "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port",
    "ESTAB  0      0      192.168.1.10:8766   192.168.1.50:51234",
    "\t cubic wscale:7,7 rto:204 rtt:3.5/1.2 mss:1448 rcv_rtt:12.5 rcv_space:14480",
    "\t bytes_acked:100 retrans:1/3 lost:2 lastrcv:120",
]
SNMP = [
    "Ip: Forwarding DefaultTTL",
    "Ip: 2 64",
    "Tcp: RtoAlgorithm InSegs OutSegs RetransSegs InErrs OutRsts",
    "Tcp: 1 1000 900 7 0 2",
]
NETSTAT = [
    "TcpExt: SyncookiesSent TCPOFOQueue TCPOFODrop TCPTimeouts",
    "TcpExt: 0 11 1 4",
]
WIRELESS = [
    "Inter-| sta-|   Quality        |   Discarded packets",
    " face | tus | link level noise |  nwid  crypt",
    " wlan0: 0000   70.  -40.  -256        0      0",
]


def test_parse_ss_reads_retransmissions_losses_and_timers():
    [conn] = r2_sampler.parse_ss(SS)
    assert conn == {
        "state": "ESTAB",
        "retrans_now": 1,
        "retrans_total": 3,
        "lost": 2,
        "rto_ms": 204.0,
        "rcv_rtt_ms": 12.5,
        "lastrcv_ms": 120,
    }
    assert r2_sampler.parse_ss(SS[:1]) == []  # header only: no connection
    assert r2_sampler.parse_ss([]) is None  # nothing printed: not measured
    [bare] = r2_sampler.parse_ss(["ESTAB 0 0 a b", "\t cubic"])
    assert bare["lost"] is None and bare["rto_ms"] is None and bare["retrans_total"] is None


def test_parse_tables_wireless_iw_sched_and_self():
    assert r2_sampler.parse_table(SNMP, "Tcp", r2_sampler.TCP_FIELDS)["RetransSegs"] == 7
    ofo = r2_sampler.parse_table(NETSTAT, "TcpExt", r2_sampler.TCPEXT_FIELDS)
    assert ofo["TCPOFOQueue"] == 11 and ofo["TCPTimeouts"] == 4 and ofo["TCPOFOMerge"] is None
    assert r2_sampler.parse_table(["Ip: a", "Ip: 1"], "Tcp", ["x"]) is None
    assert r2_sampler.parse_wireless(WIRELESS) == {
        "interface": "wlan0",
        "link": 70.0,
        "level_dbm": -40.0,
        "noise_dbm": -256.0,
    }
    assert r2_sampler.parse_wireless(WIRELESS[:2]) is None
    iw = r2_sampler.parse_iw(
        ["Station aa:bb", "\tsignal:  \t-52 dBm", "\ttx retries:\t12", "\ttx bitrate:\t72.2 MBit/s"]
    )
    assert iw == {"signal_dbm": -52.0, "tx_retries": 12, "tx_failed": None, "tx_bitrate_mbps": 72.2}
    assert r2_sampler.parse_iw([]) is None
    assert r2_sampler.parse_sched(["10 100 5 3", "11 200 6 4", "junk"]) == {
        "10": [100, 5, 3],
        "11": [200, 6, 4],
    }
    assert r2_sampler.parse_sched([]) is None
    stat_line = "123 (frame (live)) S 1 1 1 0 0 0 0 0 0 0 5 6 7 8 0 0"
    assert r2_sampler.parse_self([stat_line]) == [5, 6, 7, 8]
    assert r2_sampler.parse_self(["garbage"]) is None


def test_parse_tick_has_every_key_and_null_for_what_is_missing():
    empty = r2_sampler.parse_tick({})
    assert set(empty) == set(r2_sampler.TICK_KEYS) and all(v is None for v in empty.values())
    full = r2_sampler.parse_tick(
        {
            "ss": SS,
            "stat": ["cpu  10 20 30 40 0 0 0 0 0 0"],
            "psi": ["some avg10=0.00 avg60=0.01 avg300=0.00 total=12345"],
            "freq": ["1500000"],
            "throttled": ["throttled=0x50005"],
        }
    )
    assert full["cpu_stat"][:4] == [10, 20, 30, 40] and full["psi_cpu_some_total_us"] == 12345
    assert full["freq_khz"] == 1_500_000 and full["throttled"] == 0x50005
    assert full["snmp"] is None and full["iw"] is None


# --- budget -------------------------------------------------------------------------------


def test_budget_is_ten_percent_of_one_core():
    first, hz = (100.0, [0, 0, 0, 0]), 100
    assert r2_sampler.budget(first, (110.0, [50, 0, 0, 50]), hz)["exceeded"] is False  # 10 %
    over = r2_sampler.budget(first, (110.0, [51, 0, 0, 50]), hz)
    assert over["exceeded"] is True and over["one_core_fraction"] == 0.101
    assert r2_sampler.budget(first, first, hz)["exceeded"] is None
    assert r2_sampler.budget(None, None, hz)["exceeded"] is None
    assert r2_sampler.budget(first, (110.0, [1, 0, 0, 0]), 0)["exceeded"] is None


# --- fake ssh: the real script run against a fake /proc ----------------------------------

SS_TEXT = "\n".join(SS) + "\n"


def make_fake(tmp_path, *, ticks_per_sleep=0, iw=False):
    """A fake `ssh` (local sh) plus a fake /proc, /sys and command directory."""
    root = tmp_path / "fake"
    for rel in (
        "proc/net",
        "proc/pressure",
        "proc/4242/task/4242",
        "proc/4242/task/4243",
        "proc/self",
        "sys/devices/system/cpu/cpu0/cpufreq",
        "bin",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
    files = {
        "proc/uptime": "1000.00 3000.00\n",
        "proc/stat": "cpu  10 20 30 40 0 0 0 0 0 0\ncpu0 1 2 3 4\n",
        "proc/pressure/cpu": "some avg10=0.00 avg60=0.01 avg300=0.00 total=12345\n",
        "proc/net/snmp": "\n".join(SNMP) + "\n",
        "proc/net/netstat": "\n".join(NETSTAT) + "\n",
        "proc/net/wireless": "\n".join(WIRELESS) + "\n",
        "proc/4242/task/4242/schedstat": "1000 50 3\n",
        "proc/4242/task/4243/schedstat": "2000 70 4\n",
        "proc/self/stat": "1 (sh) S 1 1 1 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
        "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "1500000\n",
        "ss.txt": SS_TEXT,
        "count": "0\n",
    }
    for rel, text in files.items():
        (root / rel).write_text(text)
    tools = {
        "ss": f'cat "{root}/ss.txt"',
        "pgrep": "echo 4242",
        "vcgencmd": "echo throttled=0x50005",
        # Each sleep is one tick of 0.1 s: advance the fake uptime and the shell's CPU time.
        "sleep": textwrap.dedent(
            f"""\
            c=$(cat "{root}/count"); c=$((c + 1)); echo $c > "{root}/count"
            printf '%s.%s0 0.0\\n' $((1000 + c / 10)) $((c % 10)) > "{root}/proc/uptime"
            u=$((c * {ticks_per_sleep}))
            echo "1 (sh) S 1 1 1 0 0 0 0 0 0 0 $u 0 0 0 0 0" > "{root}/proc/self/stat"
            """
        ),
    }
    if iw:
        tools["iw"] = "printf 'Station aa\\n\\tsignal:\\t-52 dBm\\n\\ttx retries:\\t12\\n'"
    for name, body in tools.items():
        path = root / "bin" / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            while [ $# -gt 0 ]; do
                if [ "$1" = sh ]; then shift; break; fi
                shift
            done
            sed -e 's#/proc#{root}/proc#g' -e 's#/sys/#{root}/sys/#g' \\
                | PATH="{root}/bin:$PATH" sh "$@"
            """
        )
    )
    fake_ssh.chmod(0o755)
    return str(fake_ssh)


def read_records(out_dir):
    lines = (out_dir / r2_sampler.OUTPUT_NAME).read_text().splitlines()
    return [json.loads(line) for line in lines]


def sample(tmp_path, seconds=2, **fake):
    out = tmp_path / "evidence"
    out.mkdir()
    argv = r2_sampler.ssh_argv("pi@raspi", "192.168.1.50", seconds, ssh=make_fake(tmp_path, **fake))
    summary = r2_sampler.run(argv, out, host="pi@raspi", camera_ip="192.168.1.50", seconds=seconds)
    return summary, read_records(out)


def test_fake_ssh_run_writes_the_jsonl_with_every_section(tmp_path):
    summary, records = sample(tmp_path)
    kinds = [r["kind"] for r in records]
    assert kinds[0] == "header" and kinds[1] == "hello" and kinds[-1] == "end"
    ticks = [r for r in records if r["kind"] == "tick"]
    assert len(ticks) == 20 and [t["n"] for t in ticks] == list(range(20))
    assert summary["ticks"] == 20 and summary["gap"] is None and summary["ssh_exit"] == 0
    assert records[0]["host"] == "raspi" and "pi@" not in json.dumps(records)
    first = ticks[0]
    assert first["pi_uptime_s"] == 1000.0 and isinstance(first["mono_ns"], int)
    assert first["ss"][0]["lost"] == 2 and first["ss"][0]["retrans_total"] == 3
    assert first["snmp"]["RetransSegs"] == 7 and first["netstat"]["TCPOFOQueue"] == 11
    assert first["wireless"]["interface"] == "wlan0"
    assert first["cpu_stat"][:4] == [10, 20, 30, 40] and first["freq_khz"] == 1_500_000
    assert first["psi_cpu_some_total_us"] == 12345 and first["throttled"] == 0x50005
    # The process is not known until the first once-a-second pgrep; then its threads show.
    assert first["sched"] is None
    assert ticks[1]["sched"] == {"4242": [1000, 50, 3], "4243": [2000, 70, 4]}
    # once-a-second sections are null between their samples (not measured)
    assert ticks[1]["snmp"] is None and ticks[1]["throttled"] is None
    assert ticks[10]["snmp"] is not None and ticks[10]["self_cpu_ticks"] is not None
    assert first["iw"] is None  # no iw on this device: null, the run goes on
    assert [t["mono_ns"] for t in ticks] == sorted(t["mono_ns"] for t in ticks)
    mode = stat.S_IMODE((tmp_path / "evidence" / r2_sampler.OUTPUT_NAME).stat().st_mode)
    assert mode == 0o600


def test_iw_is_recorded_when_the_device_has_it(tmp_path):
    _summary, records = sample(tmp_path, iw=True)
    first = next(r for r in records if r["kind"] == "tick")
    assert first["iw"]["signal_dbm"] == -52.0 and first["iw"]["tx_retries"] == 12


def test_the_sampler_records_its_own_cpu_and_stays_within_budget(tmp_path):
    summary, records = sample(tmp_path, ticks_per_sleep=1)  # 1 tick per 0.1 s = 10 % of a core
    selfs = [r["self_cpu_ticks"] for r in records if r["kind"] == "tick" and r["self_cpu_ticks"]]
    assert len(selfs) == 2 and selfs[1][0] > selfs[0][0]
    assert summary["budget"]["exceeded"] is False
    assert summary["budget"]["one_core_fraction"] == pytest.approx(0.1, abs=0.01)
    saved = r2_sampler.check_budget(tmp_path / "evidence" / r2_sampler.OUTPUT_NAME)
    assert saved == records[-1]["budget"]


def test_a_budget_overrun_is_reported_and_does_not_fail_the_run(tmp_path):
    summary, records = sample(tmp_path, ticks_per_sleep=3)  # about 30 % of a core
    assert summary["budget"]["exceeded"] is True and summary["gap"] is None
    checked = r2_sampler.check_budget(tmp_path / "evidence" / r2_sampler.OUTPUT_NAME)
    assert checked["exceeded"] is True and checked["limit"] == 0.1
    assert records[-1]["kind"] == "end"


def test_check_budget_on_a_missing_or_empty_file_is_not_an_overrun(tmp_path):
    assert r2_sampler.check_budget(tmp_path / "none.jsonl")["exceeded"] is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("not json\n{}\n")
    assert r2_sampler.check_budget(empty)["exceeded"] is None


# --- ssh trouble becomes a gap, never an exception ------------------------------------


def script_ssh(tmp_path, body):
    path = tmp_path / "ssh-stub"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def run_with(tmp_path, ssh, seconds=1):
    out = tmp_path / "evidence"
    out.mkdir()
    argv = r2_sampler.ssh_argv("pi", "192.168.1.50", seconds, ssh=ssh)
    summary = r2_sampler.run(argv, out, host="pi", camera_ip="192.168.1.50", seconds=seconds)
    return summary, read_records(out)


def test_a_failing_ssh_is_a_gap_and_does_not_raise(tmp_path):
    summary, records = run_with(tmp_path, script_ssh(tmp_path, "cat >/dev/null\nexit 255"))
    assert summary["ssh_exit"] == 255 and summary["ticks"] == 0
    assert summary["gap"] == "ssh_exit_255"
    gaps = [r for r in records if r["kind"] == "gap"]
    assert [g["reason"] for g in gaps] == ["ssh_exit_255"] and isinstance(gaps[0]["mono_ns"], int)
    assert records[-1]["budget"]["exceeded"] is None


def test_a_session_that_ends_without_the_end_marker_is_a_gap(tmp_path):
    body = "cat >/dev/null\necho '@hello 1 100'\necho '@tick 0 5.0'\necho '@ss'\nexit 0"
    summary, records = run_with(tmp_path, script_ssh(tmp_path, body))
    assert summary["gap"] == "stream_ended_early" and summary["ticks"] == 1
    assert any(r["kind"] == "tick" and r["ss"] is None for r in records)


def test_a_session_that_dies_midway_keeps_what_was_sampled(tmp_path):
    body = "cat >/dev/null\necho '@tick 0 5.0'\necho '@tick 1 5.1'\nexit 1"
    summary, records = run_with(tmp_path, script_ssh(tmp_path, body))
    assert summary["ticks"] == 2 and summary["gap"] == "ssh_exit_1"
    assert [r["kind"] for r in records][-2:] == ["gap", "end"]


def test_an_ssh_that_cannot_be_started_is_a_gap(tmp_path):
    summary, records = run_with(tmp_path, str(tmp_path / "no-such-ssh"))
    assert summary["ssh_exit"] is None and summary["gap"].startswith("spawn_failed:")
    assert [r["kind"] for r in records] == ["header", "gap", "end"]


def test_ssh_stderr_never_reaches_the_output(tmp_path):
    body = "cat >/dev/null\necho 'Permission denied: SECRET-PASSPHRASE' >&2\nexit 255"
    _summary, records = run_with(tmp_path, script_ssh(tmp_path, body))
    assert "SECRET" not in json.dumps(records)


def test_the_output_file_is_created_exclusively(tmp_path):
    sample(tmp_path)
    argv = r2_sampler.ssh_argv("pi", "192.168.1.50", 1, ssh=make_fake(tmp_path))
    with pytest.raises(FileExistsError):
        r2_sampler.run(argv, tmp_path / "evidence", host="pi", camera_ip="192.168.1.50", seconds=1)


# --- command line ---------------------------------------------------------------------------


def test_cli_r2_runs_the_sampler_and_reports_a_summary(tmp_path, capsys):
    out = tmp_path / "evidence"
    args = ["r2", "pi@raspi", "192.168.1.50", str(out), "--seconds", "1"]
    assert cli.main([*args, "--ssh", make_fake(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ticks"] == 10 and summary["gap"] is None
    assert (out / "r2-sampler.jsonl").exists()
    assert cli.main([*args, "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["limit"] == 0.1
    assert cli.main([*args, "--ssh", make_fake(tmp_path)]) == 2  # never overwrites


def test_cli_r2_rejects_bad_arguments_and_survives_a_broken_ssh(tmp_path, capsys):
    out = tmp_path / "evidence"
    assert cli.main(["r2", "pi", "not-an-ip", str(out), "--seconds", "1"]) == 2
    assert cli.main(["r2", "pi;rm", "192.168.1.50", str(out), "--seconds", "1"]) == 2
    capsys.readouterr()
    failing = script_ssh(tmp_path, "cat >/dev/null\nexit 255")
    args = ["r2", "pi", "192.168.1.50", str(out), "--seconds", "1", "--ssh", failing]
    assert cli.main(args) == 0  # an R2 loss never fails the caller
    assert json.loads(capsys.readouterr().out)["gap"] == "ssh_exit_255"
