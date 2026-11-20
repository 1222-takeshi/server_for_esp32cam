"""Command line entry points. `units` and `manifest` only write files; the rest drive
the harness on the device and are started by the generated units, never by this repo's CI.

Usage: python -m scripts.spin_on_run.cli <command> ...
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import (
    common,
    environment,
    live_guardian,
    manifest,
    offline_runner,
    post_runner,
    quality,
    r2_sampler,
    recover_guard,
    start_gate,
    system_ops,
    thread_cpu,
    units,
)


def _config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text())
    cfg["config_path"] = path
    return cfg


def _with_binaries(cfg: dict) -> dict:
    """The executables that run are exactly the ones the manifest check covers."""
    found = start_gate.binaries(cfg)
    return {**cfg, "worker": str(found["frame-worker"]), "frame_live": str(found["frame-live"])}


def _gate(
    cfg: dict, baseline: dict, out: Path, *, live: bool, thread: bool = False, ops=None
) -> dict:
    platform = offline_runner.LinuxPlatform()
    return start_gate.evaluate(
        cfg,
        live=live,
        thread=thread,
        collector=_collector(cfg),
        baseline=baseline,
        out_dir=out,
        free_bytes=platform.free_bytes,
        temp_millic=platform.temp_millic,
        ops=ops,
    )


def _collector(cfg: dict) -> environment.Collector:
    token = os.environ.get(cfg.get("admin_token_env", "ESP32CAM_ADMIN_TOKEN"))
    return environment.Collector(
        get_json=environment.http_getter(
            cfg["base_url"], token, cfg.get("camera_id", "esp32cam-01")
        ),
        unit_path=cfg["unit_path"],
    )


def _block_mode_enabled(config_path: str) -> bool:
    try:
        return bool(_config(config_path).get("block_size"))
    except (OSError, ValueError, KeyError):
        return False


def cmd_units(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    date = args.date
    if args.kind == "live":
        trial = units.trial_unit(args.seconds, date)
        env = units.live_env(
            nonce="0" * 16,
            camera_boot=1,
            seconds=args.seconds,
            credentials={"ESP32CAM_DEVICE_API_KEYS": "placeholder"},
            run_dir=args.run_dir,
        )
        run = units.live_systemd_run(trial, args.seconds, args.root, args.config, args.run_dir)
        request = units.launch_request(env, units.native_argv("frame-live", "m", "r"), run)
        common.write_json(out / "launch-request.dry-run.json", request)
        common.write_new(
            out / units.RECOVER_UNIT,
            units.recover_unit_text(trial, args.root, args.config, args.run_dir),
        )
    else:
        prefix = units.THREAD_PREFIX if args.thread else units.OFFLINE_PREFIX
        offline, post = f"{prefix}-{date}", f"{prefix}-post-{date}"
        common.write_json(
            out / "offline-systemd-run.json",
            {
                "offline": units.offline_systemd_run(
                    offline, args.root, args.config, args.run_dir, thread=args.thread,
                    runtime_max_s=7200 if _block_mode_enabled(args.config) else None,
                ),
                "post": units.post_systemd_run(post, offline, args.root, args.config, args.run_dir),
            },
        )
    print(f"wrote {out}")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    binaries = dict(item.split("=", 1) for item in args.binary)
    built = manifest.build(
        Path(__file__).parent,
        {k: Path(v) for k, v in binaries.items()},
        args.base_commit,
        args.issue,
    )
    sys.stdout.write(manifest.dump(built))
    return 0


def _offline_config(cfg: dict, out_dir: Path, *, thread: bool) -> offline_runner.Runner:
    golden = quality.load_golden(Path(cfg["golden"]).read_text())
    clk = os.sysconf("SC_CLK_TCK")
    collector = _collector(cfg)
    baseline = json.loads(Path(cfg["baseline"]).read_text())

    rechecks = []

    def deviations() -> list[str]:
        found, record = environment.deviations_with_recheck(
            baseline,
            collector.snapshot(),
            "offline",
            collector.snapshot,
            allowed_units=(cfg["offline_unit"] + ".service",),
        )
        if record["rechecked"]:
            rechecks.append(record)
            common.write_json(out_dir / f"mode-recheck.offline.{len(rechecks)}.json", record)
        return found

    platform = offline_runner.LinuxPlatform(deviations)
    run_cfg = offline_runner.Config(
        # Candidate M measures the registered unmodified worker, S the new one.
        worker=Path(cfg["pr616_worker"] if thread else cfg["worker"]),
        model=Path(cfg["model"]),
        ort=Path(cfg["ort"]),
        input_path=Path(cfg["input"]),
        golden=golden,
        out_dir=out_dir,
        clk_tck=clk,
        candidate_env=dict(cfg.get("candidate_env", {})),
        candidate_prefix=cfg.get("candidate_prefix"),
        block_size=int(cfg.get("block_size", 0) or 0),
        block_max_retries=int(cfg.get("block_max_retries", 3)),
        block_wait_s=float(cfg.get("block_wait_s", 600)),
    )
    if thread:
        thread_cpu.configure(run_cfg, platform)
    return offline_runner.Runner(run_cfg, platform)


def cmd_offline_run(args: argparse.Namespace) -> int:
    cfg = _with_binaries(_config(args.config))
    out = Path(args.out)
    baseline = json.loads(Path(cfg["baseline"]).read_text())
    gate = _gate(cfg, baseline, out, live=False, thread=args.thread)
    if not gate["ok"]:
        print(json.dumps(start_gate.refuse_offline(out, gate)["verdict"]))
        return start_gate.REFUSED_EXIT
    out.mkdir(parents=True, exist_ok=True)
    common.write_json(out / "preflight.json", gate)
    runner = _offline_config(cfg, out, thread=args.thread)
    result = runner.run()
    print(json.dumps(result.get("verdict") or result.get("outcome")))
    return 0 if not runner.codes else 1


def cmd_offline_post(args: argparse.Namespace) -> int:
    cfg = _config(args.config)
    out = Path(args.out)
    baseline = json.loads(Path(cfg["baseline"]).read_text())
    if args.step == "launch":
        unit = cfg["post_unit"]
        command = units.post_systemd_run(
            unit, cfg["offline_unit"], cfg["root"], args.config, str(out)
        )
        return subprocess.run(command, check=False).returncode  # noqa: S603
    if args.step == "run":
        result = post_runner.run_post(
            out,
            _collector(cfg),
            baseline,
            offline_unit=cfg["offline_unit"] + ".service",
            post_unit=cfg["post_unit"] + ".service",
            frozen_changes=start_gate.frozen_changes(cfg),
        )
        return 0 if result["pass"] else 1
    if args.step == "finalize":
        return 1 if post_runner.finalize_post(out, os.environ.get("SERVICE_RESULT", "")) else 0
    final = post_runner.final_check(
        out,
        _collector(cfg),
        baseline,
        frozen_changes=start_gate.frozen_changes(cfg),
    )
    print(json.dumps(final["verdict"]))
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    cfg = _config(args.config)
    run_dir = Path(args.out)
    baseline = json.loads(Path(cfg["baseline"]).read_text())
    ops = system_ops.SystemOps(
        trial_unit=cfg["trial_unit"],
        self_unit=units.RECOVER_UNIT,
        base_url=cfg["base_url"],
        admin_token=os.environ.get(cfg.get("admin_token_env", "ESP32CAM_ADMIN_TOKEN")),
        camera_id=cfg.get("camera_id", "esp32cam-01"),
    )
    ops.collector = _collector(cfg)
    if args.step == "check":
        problems = recover_guard.check_only(ops, baseline)
        print(json.dumps(problems))
        return 1 if problems else 0
    if args.step == "enqueue":
        info = {k: os.environ.get(k) for k in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")}
        common.write_json(run_dir / "stop-info.json", {k: v for k, v in info.items() if v})
        os.execvp("systemctl", ["systemctl", "--user", "start", "--no-block", units.RECOVER_UNIT])
    if args.step == "recover":
        guard = recover_guard.Guard(ops, run_dir, baseline, cfg["seconds"])
        return recover_guard.exit_code(guard.run())
    cfg = _with_binaries(cfg)
    gate = _gate(cfg, baseline, run_dir, live=True, ops=ops)
    if not gate["ok"]:
        start_gate.refuse_live(run_dir, gate)
        return start_gate.REFUSED_EXIT
    return _execute(cfg, run_dir, baseline, ops)


def _execute(cfg: dict, run_dir: Path, baseline: dict, ops: system_ops.SystemOps) -> int:
    collector = ops.collector

    def read(now: float) -> live_guardian.Reading:
        platform = offline_runner.LinuxPlatform()
        try:
            temp, throttled = platform.temp_millic(), platform.throttled()
        except (OSError, ValueError, subprocess.SubprocessError):
            temp = throttled = None
        try:
            free = platform.free_bytes(run_dir)
        except OSError:
            free = None
        try:
            fast = collector.snapshot_local()  # never the service API: it is stopped
        except Exception:  # noqa: BLE001
            fast = None
        return live_guardian.Reading(temp, throttled, free, fast)

    def spawn(argv, env, stdout, stderr):
        return subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)

    env = json.loads(Path(cfg["launch_env"]).read_text())
    units.validate_live_env(env)
    units.check_frame_log(env, str(run_dir))
    summary = live_guardian.execute(
        run_dir=run_dir,
        seconds=cfg["seconds"],
        argv=units.native_argv(cfg["frame_live"], cfg["model"], cfg["ort"]),
        env=env,
        nonce=env["ESP32CAM_RUST_TRIAL_NONCE"],
        checker_factory=lambda d: live_guardian.Checker(
            cfg["seconds"], d, baseline, cfg["trial_unit"]
        ),
        io=live_guardian.IO(
            time.monotonic,
            time.sleep,
            read,
            spawn,
            lambda: int(time.time() * 1000),
            ops.stop_service,
        ),
    )
    return 0 if not summary["codes"] else 1


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="spin_on_run")
    sub = top.add_subparsers(dest="command", required=True)
    u = sub.add_parser("units", help="write unit text and a launch-request dry run")
    u.add_argument("--kind", choices=["offline", "live"], required=True)
    u.add_argument("--seconds", type=int, choices=[600, 3600], default=600)
    u.add_argument("--date", required=True)
    u.add_argument("--out", required=True)
    u.add_argument("--root", default="/home/ubuntu/spin-on-run")
    u.add_argument("--config", default="/home/ubuntu/spin-on-run/config.json")
    u.add_argument("--run-dir", default="/home/ubuntu/spin-on-run/run")
    u.add_argument("--thread", action="store_true", help="candidate M units")
    u.set_defaults(handler=cmd_units)
    m = sub.add_parser("manifest", help="print the prepared manifest as JSON")
    m.add_argument("--binary", action="append", default=[], help="label=path")
    m.add_argument("--base-commit", required=True)
    m.add_argument("--issue", type=int)
    m.set_defaults(handler=cmd_manifest)
    o = sub.add_parser("offline-run")
    o.add_argument("config")
    o.add_argument("out")
    o.add_argument("--thread", action="store_true", help="candidate M instead of S")
    o.set_defaults(handler=cmd_offline_run)
    p = sub.add_parser("offline-post")
    p.add_argument("config")
    p.add_argument("out")
    p.add_argument("step", choices=["launch", "run", "finalize", "final-check"])
    p.set_defaults(handler=cmd_offline_post)
    lv = sub.add_parser("live")
    lv.add_argument("config")
    lv.add_argument("out")
    lv.add_argument("step", choices=["execute", "enqueue", "recover", "check"])
    lv.set_defaults(handler=cmd_live)
    r2_sampler.add_subcommand(sub)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
