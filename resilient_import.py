#!/usr/bin/env python3
"""Keep re-launching `export_to_sqlite.py <source>` in short, capped chunks
until it's actually done, instead of one long-lived run.

Why this exists (2026-08-06): three separate long-lived export_to_sqlite.py
background runs today died mid-run with no Python traceback, no "FAILED"
line, no OS sleep/hibernate event, no crash logged, and no antivirus action
around the time of death -- ruled all of that out directly against Windows
Event Log and the process list. The process was just gone. Most likely
explanation left standing: something in how this machine's Claude-Code-CLI
background-task tracking manages long-lived detached processes was reaping
them, independent of anything export_to_sqlite.py itself was doing.

Two independent mitigations, both live here:

1. Detachment: launch *this* wrapper (not export_to_sqlite.py directly) as a
   real standalone OS process -- e.g. via PowerShell's `Start-Process`, which
   is not a child of the calling shell's job object the way backgrounding
   within a session is. If it really is the harness's background-task
   tracking killing long-lived tracked processes, a process that was never
   tracked by it in the first place should be immune. See the module's
   "Usage" section below for the exact launch invocation.

2. Short chunks: even if something *does* kill the underlying python.exe
   again, running in --max-runtime-minutes-capped chunks means the most that
   gets lost is one chunk's uncommitted tail, not hours of progress. This
   wrapper just keeps relaunching a fresh chunk until export_to_sqlite.py
   itself reports there was nothing left to do, or --max-total-minutes runs
   out.

export_to_sqlite.py's own exit code tells this loop what happened to a
chunk:
    0  -- the whole filtered candidate list was worked through; nothing left
    3  -- stopped early because --max-runtime-minutes ran out; more to do
    anything else (including a negative/signal-style code, or the process
    just vanishing) -- treated as a crash/external kill; retried anyway,
    after a longer cooldown, up to --max-consecutive-failures in a row.

Usage (detached, survives this Claude Code session ending):
    powershell -Command "Start-Process -FilePath python -ArgumentList \
      'resilient_import.py chatgpt --chunk-minutes 15 --max-total-minutes 240 --notify' \
      -WorkingDirectory 'C:\\github' -WindowStyle Hidden \
      -RedirectStandardOutput 'C:\\github\\chatgpt_resilient.log' \
      -RedirectStandardError 'C:\\github\\chatgpt_resilient.err.log'"

Or run it directly (inherits the caller's stdout/stderr) for a quick test:
    python resilient_import.py chatgpt --chunk-minutes 5 --max-total-minutes 15
"""
import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
EXPORT_SCRIPT = REPO_ROOT / "export_to_sqlite.py"

# Same topic/knobs as export_to_sqlite.py's own ntfy_notify() -- see that
# module for the override/disable mechanism (NTFY_TOPIC="" disables).
sys.path.insert(0, str(REPO_ROOT))
from export_to_sqlite import ntfy_notify  # noqa: E402


STATUS_DONE = 0
STATUS_STOPPED_EARLY = 3


def run_chunk(source, *, chunk_minutes, port, db, batch_size, notify, limit=None):
    cmd = [
        sys.executable, str(EXPORT_SCRIPT),
        "--max-runtime-minutes", str(chunk_minutes),
        "--port", str(port), "--db", str(db), "--batch-size", str(batch_size),
    ]
    if notify:
        cmd.append("--notify")
    if limit:
        cmd += ["--limit", str(limit)]
    cmd.append(source)
    print(f"[resilient_import] launching chunk: {' '.join(cmd)}", flush=True)
    # No stdout/stderr= here on purpose -- inherits this wrapper's own
    # streams, so a chunk's [i/N] progress lines land in the same log file
    # the wrapper was launched with (see the module docstring's Usage).
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", choices=["claude", "chatgpt"])
    parser.add_argument("--chunk-minutes", type=float, default=15,
                         help="--max-runtime-minutes passed to each chunk (default 15)")
    parser.add_argument("--max-total-minutes", type=float, default=240,
                         help="Give up (report incomplete) after this much total wall time (default 240 = 4h)")
    parser.add_argument("--cooldown-seconds", type=float, default=30,
                         help="Pause between a clean/stopped-early chunk and the next one (default 30)")
    parser.add_argument("--crash-cooldown-seconds", type=float, default=120,
                         help="Longer pause after a chunk that crashed/vanished, to ride out whatever "
                              "caused it rather than immediately repeating it (default 120)")
    parser.add_argument("--max-consecutive-failures", type=int, default=5,
                         help="Give up after this many chunk crashes in a row (default 5)")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--db", default=str(REPO_ROOT / "conversations.db"))
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                         help="Passed through to each chunk -- mainly for testing this wrapper itself; "
                              "leave unset for a real run so each chunk works the full backlog")
    args = parser.parse_args()

    sys.stdout.reconfigure(errors="replace", line_buffering=True)
    sys.stderr.reconfigure(errors="replace", line_buffering=True)

    start = time.monotonic()
    deadline = start + args.max_total_minutes * 60
    consecutive_failures = 0
    chunk_num = 0

    if args.notify:
        ntfy_notify(
            f"{args.source} resilient import starting",
            f"chunks of {args.chunk_minutes:.0f}m, capped at {args.max_total_minutes:.0f}m total",
            priority="low", tags="rocket",
        )

    while True:
        chunk_num += 1
        remaining_total = deadline - time.monotonic()
        if remaining_total <= 0:
            print(f"[resilient_import] hit --max-total-minutes ({args.max_total_minutes}m) -- giving up for now, "
                  f"rerun to resume", flush=True)
            if args.notify:
                ntfy_notify(f"{args.source} resilient import: time budget exhausted",
                            f"Stopped after {chunk_num - 1} chunks, {(time.monotonic() - start) / 60:.0f}m total -- "
                            f"rerun to resume", priority="default", tags="hourglass_flowing_sand")
            sys.exit(1)

        chunk_minutes = min(args.chunk_minutes, remaining_total / 60)
        rc = run_chunk(args.source, chunk_minutes=chunk_minutes, port=args.port, db=args.db,
                        batch_size=args.batch_size, notify=args.notify, limit=args.limit)

        if rc == STATUS_DONE:
            print(f"[resilient_import] chunk {chunk_num}: done -- nothing left to import", flush=True)
            if args.notify:
                ntfy_notify(f"{args.source} resilient import: complete",
                            f"Finished after {chunk_num} chunk(s), "
                            f"{(time.monotonic() - start) / 60:.0f}m total",
                            priority="default", tags="white_check_mark")
            sys.exit(0)
        elif rc == STATUS_STOPPED_EARLY:
            print(f"[resilient_import] chunk {chunk_num}: stopped early (time budget), "
                  f"more to do -- cooling down {args.cooldown_seconds:.0f}s", flush=True)
            consecutive_failures = 0
            time.sleep(args.cooldown_seconds)
        else:
            consecutive_failures += 1
            print(f"[resilient_import] chunk {chunk_num}: exited abnormally (code {rc}) -- "
                  f"treating as a crash/external kill, {consecutive_failures}/{args.max_consecutive_failures} "
                  f"consecutive -- cooling down {args.crash_cooldown_seconds:.0f}s", flush=True)
            if consecutive_failures >= args.max_consecutive_failures:
                print(f"[resilient_import] {consecutive_failures} consecutive chunk failures -- giving up",
                      flush=True)
                if args.notify:
                    ntfy_notify(f"{args.source} resilient import: giving up",
                                f"{consecutive_failures} chunks crashed in a row -- needs a human look",
                                priority="high", tags="x")
                sys.exit(2)
            time.sleep(args.crash_cooldown_seconds)


if __name__ == "__main__":
    main()
