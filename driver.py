#!/usr/bin/env python3
"""Drive one stream-json `claude -p` session of the subagent-resume hand-back repro.

Run with `python3 -I driver.py ...` (isolated mode: no user site, no cwd on sys.path).

Usage:
  python3 -I driver.py --variant W|L|H [--label NAME] [--run-dir DIR]
                       [--sleep SECONDS] [--timeout SECONDS] [--quiet SECONDS]
                       [--claude PATH] [--agents-template FILE]
  python3 -I driver.py --variant CLI [--label NAME]       (delegates to run-cli.sh)
  python3 -I driver.py --variant W --run-dir DIR --render-only   (print the agents JSON)
  python3 -I driver.py --print-allowed                           (print the --allowedTools value)

Variants (one resumer procedure, three endings):
  W    the resumer ends its turn with plain text, no tool call, right after its SendMessage.
  L    W, and the resumed child's turn lasts at least 300 s (sleep 300 after its shuf).
  H    the resumer calls SubagentHandback itself right after its SendMessage.
  CLI  W on the interactive CLI through tmux (run-cli.sh).

Layout: $REPRO_DIR (default: this script's directory) holds agents.json (the template);
each run writes $REPRO_DIR/runs/<label>/ with
  stream.jsonl   every stdout line of claude, stamped with its receipt time
  stderr.txt     stderr lines, stamped
  agents.json    the rendered agents of this run
  meta.json      variant, version, flags, timing, models (no paths of the machine)
  cwd/           the fresh, empty working directory of the session
  nonce.num, r2.txt   written by the child during the run
"""

import argparse
import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPRO_DIR = os.path.abspath(os.environ.get("REPRO_DIR") or HERE)

ALLOWED_TOOLS = [
    "Agent",
    "SendMessage",
    "Bash(shuf *)",
    "Bash(cat *)",
    "Bash(sleep *)",
]

WAIT_PLAIN = (
    "Right after the SendMessage result arrives, your very next action must be to end your "
    "turn with one plain-text line: waiting for the child. Do not call any tool in that turn "
    "and do not poll, sleep or read anything while you wait. If you are later asked to call "
    "SubagentHandback, call it with whatever you know at that moment. If the child's reply "
    "reaches you, finish with a plain-text report that quotes the child's reply verbatim."
)
WAIT_HANDBACK = (
    "Right after the SendMessage result arrives, your very next action must be to call "
    "SubagentHandback yourself, with the message: handed back right after the SendMessage; "
    "child reply not received yet. Do not call any other tool first and do not wait."
)
SLEEP_STEP = "After the shuf command, run exactly one Bash command: sleep {s}\n"

VARIANTS = {
    "W": {"wait": WAIT_PLAIN, "sleep": 0},
    "L": {"wait": WAIT_PLAIN, "sleep": 300},
    "H": {"wait": WAIT_HANDBACK, "sleep": 0},
}

# The only three environment names dropped from the nested run; every other outer-session
# variable is inherited (see README section 3).
DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def die(msg, code=2):
    print(f"driver.py: {msg}", file=sys.stderr)
    sys.exit(code)


def substitute(node, mapping):
    if isinstance(node, str):
        for k, v in mapping.items():
            node = node.replace(k, v)
        return node
    if isinstance(node, list):
        return [substitute(x, mapping) for x in node]
    if isinstance(node, dict):
        return {k: substitute(v, mapping) for k, v in node.items()}
    return node


def render_agents(template_path, variant, run_dir, sleep_s):
    with open(template_path, encoding="utf-8") as fh:
        agents = json.load(fh)
    spec = VARIANTS[variant]
    mapping = {
        "{{RUN_DIR}}": run_dir,
        "{{WAIT}}": spec["wait"],
        "{{CHILD_SLEEP}}": SLEEP_STEP.format(s=sleep_s) if sleep_s > 0 else "",
    }
    out = substitute(agents, mapping)
    if "{{" in json.dumps(out):
        die("unresolved placeholder left in the rendered agents")
    return out


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pump(stream, tag, q):
    for line in iter(stream.readline, b""):
        q.put((tag, time.time(), line))
    q.put((tag, time.time(), None))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", choices=["W", "L", "H", "CLI"])
    ap.add_argument("--label", help="run name; default <variant>-<UTC timestamp>")
    ap.add_argument("--run-dir", help="run directory; default $REPRO_DIR/runs/<label>")
    ap.add_argument("--sleep", type=int, help="child sleep in the resumed turn (default 300 for L, else 0)")
    ap.add_argument("--timeout", type=int, help="hard limit in seconds (default 900 + sleep)")
    ap.add_argument("--quiet", type=int, help="stop after this many seconds with no output once the root has "
                    "reported at least once (default 60 + sleep)")
    ap.add_argument("--claude", default="claude", help="claude executable")
    ap.add_argument("--agents-template", default=os.path.join(REPRO_DIR, "agents.json"))
    ap.add_argument("--render-only", action="store_true", help="print the rendered agents JSON and exit")
    ap.add_argument("--print-allowed", action="store_true", help="print the --allowedTools value and exit")
    args = ap.parse_args()

    if args.print_allowed:
        print(",".join(ALLOWED_TOOLS))
        return 0
    if not args.variant:
        ap.error("--variant is required")

    if args.variant == "CLI":
        script = os.path.join(REPRO_DIR, "run-cli.sh")
        argv = ["bash", script]
        if args.label:
            argv.append(args.label)
        os.execvp("bash", argv)

    sleep_s = VARIANTS[args.variant]["sleep"] if args.sleep is None else args.sleep
    label = args.label or f"{args.variant}-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    run_dir = os.path.abspath(args.run_dir or os.path.join(REPRO_DIR, "runs", label))

    if args.render_only:
        agents = render_agents(args.agents_template, args.variant, run_dir, sleep_s)
        print(json.dumps(agents, separators=(",", ":"), ensure_ascii=False))
        return 0

    if os.path.exists(os.path.join(run_dir, "stream.jsonl")):
        die(f"{label}: a stream already exists in the run directory; pick another --label")
    cwd = os.path.join(run_dir, "cwd")
    os.makedirs(cwd, exist_ok=True)
    if os.listdir(cwd):
        die("the run's working directory is not empty")

    agents = render_agents(args.agents_template, args.variant, run_dir, sleep_s)
    agents_path = os.path.join(run_dir, "agents.json")
    with open(agents_path, "w", encoding="utf-8") as fh:
        json.dump(agents, fh, indent=2, ensure_ascii=False)

    timeout = args.timeout if args.timeout is not None else 900 + sleep_s
    quiet = args.quiet if args.quiet is not None else 60 + sleep_s

    cmd = [
        args.claude, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--forward-subagent-text",
        "--agent", "repro-root",
        "--agents", agents_path,
        "--setting-sources", "project,local",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
    ]

    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

    try:
        version = subprocess.run([args.claude, "--version"], capture_output=True, text=True,
                                 env=env, cwd=cwd, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        die(f"cannot run {args.claude} --version: {exc}")

    t0 = time.time()
    meta = {
        "variant": args.variant,
        "label": label,
        "started_at": now_iso(),
        "start_epoch": t0,
        "claude_version_flag": version,
        "child_sleep_seconds": sleep_s,
        "timeout_seconds": timeout,
        "quiet_seconds": quiet,
        "models": {name: spec.get("model") for name, spec in agents.items()},
        "argv": [("$RUN_DIR/agents.json" if a == agents_path else a) for a in cmd],
        "env_set": {k: env[k] for k in ("DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
                                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY")},
        "env_dropped": [k for k in DROP_ENV if k in os.environ],
        "env_names_present": sorted(k for k in env if k.startswith(("CLAUDE", "ANTHROPIC"))),
    }

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env)
    q = queue.Queue()
    threading.Thread(target=pump, args=(proc.stdout, "out", q), daemon=True).start()
    threading.Thread(target=pump, args=(proc.stderr, "err", q), daemon=True).start()

    start_msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "START"}]}}
    out_fh = open(os.path.join(run_dir, "stream.jsonl"), "w", encoding="utf-8")
    err_fh = open(os.path.join(run_dir, "stderr.txt"), "w", encoding="utf-8")

    def stamp(extra, t):
        rec = {"t": round(t, 6), "rel": round(t - t0, 6)}
        rec.update(extra)
        out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out_fh.flush()

    sent_t = time.time()
    try:
        proc.stdin.write((json.dumps(start_msg) + "\n").encode())
        proc.stdin.flush()
    except OSError:
        pass
    stamp({"sent": start_msg}, sent_t)

    results = 0
    last_line_t = time.time()
    open_pipes = 2
    timed_out = False
    stopped_quiet = False

    while True:
        now = time.time()
        if now - t0 > timeout:
            timed_out = True
            break
        if results >= 1 and now - last_line_t >= quiet:
            stopped_quiet = True
            break
        try:
            tag, t, line = q.get(timeout=0.5)
        except queue.Empty:
            if open_pipes == 0 and proc.poll() is not None:
                break
            continue
        if line is None:
            open_pipes -= 1
            if open_pipes == 0 and proc.poll() is not None and q.empty():
                break
            continue
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        if tag == "err":
            err_fh.write(f"{t:.6f} {text}\n")
            err_fh.flush()
            continue
        if not text.strip():
            continue
        last_line_t = t
        try:
            obj = json.loads(text)
        except ValueError:
            stamp({"raw": text}, t)
            continue
        stamp({"msg": obj}, t)
        if isinstance(obj, dict) and obj.get("type") == "result":
            results += 1

    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    while not q.empty():
        tag, t, line = q.get_nowait()
        if line is not None and tag == "out":
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            try:
                stamp({"msg": json.loads(text), "late": True}, t)
            except ValueError:
                stamp({"raw": text, "late": True}, t)
    out_fh.close()
    err_fh.close()

    meta.update({
        "ended_at": now_iso(),
        "elapsed_seconds": round(time.time() - t0, 3),
        "result_messages": results,
        "timed_out": timed_out,
        "stopped_quiet": stopped_quiet,
        "exit_code": proc.returncode,
    })
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    print(f"run {label}: {meta['elapsed_seconds']}s, results={results}, timed_out={timed_out}, "
          f"exit={proc.returncode}; score with: python3 -I score.py runs/{label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
