#!/usr/bin/env python3
"""fleet.py - minimal dispatch -> monitor -> collect -> feedback loop for coding agents.

One `tick` is one idempotent reconcile pass. Run it every few minutes:
    python3 fleet.py tick                  # from cron / launchd / `/loop 10m python3 fleet.py tick`
    python3 fleet.py watch 300             # or a foreground loop inside tmux

Per tick:
  1. monitor   running cards: finished? crashed? over time budget (kill own process group)?
  2. collect   finished cards: BLOCKED.md -> blocked; otherwise commit leftovers and run the
               card's acceptance command. The gate decides; the agent's own report does not.
  3. feedback  failed gate and attempts left -> requeue with the gate output in the next prompt
               (optionally on the fallback runtime); otherwise -> failed / review / done
  4. dispatch  fill free slots per runtime (claude / codex / ...) from the queue, respecting
               priority and depends_on; each card gets its own git worktree and branch
  5. report    rewrite STATUS.md, append events.jsonl, run the notify hook for human states

Layout under --home (default ./fleet):
  config.json        runtimes (argv templates), per-runtime caps, defaults, notify hook
  cards/<id>.md      task cards = the queue (front matter + body)
  state/<id>.json    machine state per card
  runs/<id>/<n>/     prompt, stdout/stderr, exit_code, last message, gate.log per attempt
  worktrees/<id>/    isolated git worktree (branch fleet/<id>)
  STATUS.md          the board;  events.jsonl  append-only transitions

Human commands: requeue <id> [--note ...] (note is fed into the next prompt), stop <id>, status.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HUMAN_STATES = ("review", "blocked", "failed")
TAIL = 3000

DEFAULT_CONFIG = {
    "caps": {"claude": 4, "codex": 3},
    "defaults": {"runtime": "claude", "max_attempts": 2, "timeout_min": 90,
                 "gate_timeout_min": 20, "base": "HEAD", "risk": "low", "priority": 100},
    "runtimes": {
        # prompt arrives on stdin; {model} {worktree} {gitdir} {run} are substituted per launch
        "claude": {
            "model": "sonnet",
            "argv": ["claude", "-p", "--permission-mode", "bypassPermissions",
                     "--model", "{model}", "--output-format", "json"],
            "last_message": "json:result",
        },
        "codex": {
            "model": "gpt-5.6-sol",
            # explicit CPA provider on every launch (never rely on ~/.codex/config.toml); non-fast tier
            "argv": ["codex", "exec", "-C", "{worktree}", "-s", "workspace-write",
                     "--add-dir", "{gitdir}", "--skip-git-repo-check",
                     "-o", "{run}/last_message.txt", "-m", "{model}",
                     "-c", "service_tier=\"default\"",
                     "-c", "model_provider=\"cpa\"",
                     "-c", "model_providers.cpa.name=\"CPA\"",
                     "-c", "model_providers.cpa.base_url=\"http://127.0.0.1:8317/v1\"",
                     "-c", "model_providers.cpa.wire_api=\"responses\"",
                     "-c", "model_providers.cpa.env_key=\"CPA_API_KEY\"",
                     "-c", "model_providers.cpa.requires_openai_auth=false",
                     "-"],
            "last_message": "file:last_message.txt",
        },
    },
    # optional shell run in the worktree after the gate passes, e.g. push + open a PR/MR:
    #   "git push -q -u origin HEAD && gh pr create --fill"   /   "... && glab mr create --fill --yes"
    # {id} and {branch} are substituted; a card may override it with its own `on_pass:` line
    "on_pass": "",
    # argv run on transitions into done/review/blocked/failed; {id} {status} {reason} substituted
    "notify": ["osascript", "-e", "display notification \"{id}: {status}\" with title \"fleet\""],
}

EXAMPLE_CARD = """---
id: example-add-mul
repo: /path/to/repo
runtime: claude
fallback: codex
model: sonnet
risk: low
priority: 100
max_attempts: 2
timeout_min: 60
accept: python3 -m unittest -q
---
# Add mul(a, b)

## Goal
calc.py gets mul(a, b) returning a * b, with a unit test.

## Scope
- May change: calc.py, tests/
- Must not touch: anything else
"""

CONTRACT = """

---
## Execution contract (appended by the dispatcher; overrides your other habits)
- You are an unattended worker. Do not ask questions or wait for confirmation. If something is
  ambiguous, pick the most reasonable option and state the assumption in your final report.
- Only modify files inside this git worktree ({worktree}, branch {branch}). Do not write anywhere
  outside it (no notes, no memory files, no other repos).
- Done means: running `{accept}` from the worktree root exits 0. Run it yourself before finishing.
- Commit your work on the current branch. If committing fails, leave the changes in place; the
  dispatcher commits leftovers.
- Only if truly blocked (missing credentials, change needed outside scope, acceptance command itself
  broken): write BLOCKED.md at the worktree root (what blocks you, who must decide what, what you
  tried) and stop.
- End with a 3-5 line report: what you did, assumptions, acceptance result.
"""


def now():
    return time.time()


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


def tail(text, n=TAIL):
    text = text or ""
    return text if len(text) <= n else "...\n" + text[-n:]


def write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def parse_card(path):
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            body = text[end + 4:].lstrip("\n")
            for line in text[4:end].splitlines():
                key, sep, val = line.partition(":")
                if not sep or not key.strip() or line.lstrip().startswith("#"):
                    continue
                val = val.strip()
                if val.startswith("[") and val.endswith("]"):
                    val = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
                elif len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                    val = val[1:-1]
                meta[key.strip()] = val
    meta.setdefault("id", path.stem)
    return meta, body


class Fleet:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.cards_dir = self.home / "cards"
        self.state_dir = self.home / "state"
        self.runs_dir = self.home / "runs"
        self.wt_dir = self.home / "worktrees"
        cfg_path = self.home / "config.json"
        self.cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else DEFAULT_CONFIG
        self.defaults = {**DEFAULT_CONFIG["defaults"], **self.cfg.get("defaults", {})}
        self.problems = []

    # ---------- state ----------
    def load_cards(self):
        cards, seen = {}, set()
        for p in sorted(self.cards_dir.glob("*.md")):
            try:
                meta, body = parse_card(p)
            except (OSError, UnicodeDecodeError) as e:
                self.problems.append(f"{p.name}: unreadable ({e})")
                continue
            cid = meta["id"]
            if cid in seen:
                self.problems.append(f"{p.name}: duplicate id {cid}, ignored")
                continue
            seen.add(cid)
            cards[cid] = (meta, body)
        return cards

    def opt(self, meta, key):
        return meta.get(key, self.defaults.get(key))

    def load_state(self, cid):
        p = self.state_dir / f"{cid}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {"id": cid, "status": "queued", "attempts": 0, "history": [], "feedback": [],
                "created_at": now()}

    def save_state(self, st):
        write_atomic(self.state_dir / f"{st['id']}.json", json.dumps(st, ensure_ascii=False, indent=2))

    def transition(self, st, status, reason=""):
        old = st.get("status")
        reason = " ".join(str(reason).split())  # one line, so STATUS.md and events stay readable
        st["status"], st["reason"], st["updated_at"] = status, reason, now()
        self.save_state(st)
        with open(self.home / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": iso(now()), "id": st["id"], "from": old, "to": status,
                                 "attempt": st.get("attempts"), "reason": reason[:300]},
                                ensure_ascii=False) + "\n")
        if status in HUMAN_STATES + ("done",) and self.cfg.get("notify"):
            argv = [a.replace("{id}", st["id"]).replace("{status}", status).replace("{reason}", reason[:120])
                    for a in self.cfg["notify"]]
            try:
                subprocess.run(argv, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass

    # ---------- processes ----------
    @staticmethod
    def owned(pid, marker):
        """True only if pid is alive AND its command line contains our run dir (guards pid reuse)."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)  # reap if it is our own child (watch mode)
        except ChildProcessError:
            pass
        r = subprocess.run(["ps", "-ww", "-o", "stat=,command=", "-p", str(pid)], capture_output=True, text=True)
        out = r.stdout.strip()
        return bool(out) and not out.startswith("Z") and marker in out

    @staticmethod
    def kill_group(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.killpg(pid, 0)
                except (ProcessLookupError, PermissionError):
                    return

    # ---------- phases ----------
    def monitor(self, cards):
        for cid in list(cards):
            st = self.load_state(cid)
            if st["status"] != "running":
                continue
            try:
                self.check_running(cards[cid], st)
            except (RuntimeError, OSError, ValueError) as e:
                self.transition(st, "failed", f"collector error: {str(e)[:300]}")

    def check_running(self, card, st):
        run = Path(st["run_dir"])
        timeout = float(self.opt(card[0], "timeout_min")) * 60
        if (run / "exit_code").exists():
            rc_text = (run / "exit_code").read_text().strip()
            self.collect(card, st, int(rc_text) if rc_text.lstrip("-").isdigit() else -1, "")
        elif not self.owned(st["pid"], str(run)):
            self.collect(card, st, -1, "agent process vanished without exit code")
        elif now() - st["started_at"] > timeout:
            self.kill_group(st["pid"])
            self.collect(card, st, -1, f"timeout after {timeout / 60:g} min, killed")

    def last_message(self, rt_name, run):
        spec = self.cfg["runtimes"].get(rt_name, {}).get("last_message", "")
        try:
            if spec.startswith("json:"):
                data = json.loads((run / "stdout.txt").read_text(encoding="utf-8", errors="replace"))
                return str(data.get(spec[5:], "")), data.get("total_cost_usd")
            if spec.startswith("file:"):
                return (run / spec[5:]).read_text(encoding="utf-8", errors="replace"), None
        except (OSError, ValueError):
            pass
        return tail((run / "stdout.txt").read_text(encoding="utf-8", errors="replace")) if (run / "stdout.txt").exists() else "", None

    def run_gate(self, accept, wt, minutes):
        p = subprocess.Popen(["/bin/sh", "-c", accept], cwd=wt, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=minutes * 60)
            return p.returncode, out
        except subprocess.TimeoutExpired:
            self.kill_group(p.pid)
            out, _ = p.communicate()
            return 124, (out or "") + f"\n[gate timeout after {minutes} min]"

    def collect(self, card, st, rc, reason):
        meta, _ = card
        run, wt = Path(st["run_dir"]), Path(st["worktree"])
        msg, cost = self.last_message(st["runtime"], run)
        entry = {"attempt": st["attempts"], "run": run.name, "runtime": st["runtime"], "exit_code": rc,
                 "started": iso(st["started_at"]), "ended": iso(now()), "cost_usd": cost,
                 "report": tail(msg, 800)}
        blocked = wt / "BLOCKED.md"
        if blocked.exists():
            text = blocked.read_text(encoding="utf-8", errors="replace")
            (run / "BLOCKED.md").write_text(text, encoding="utf-8")
            blocked.unlink()
            entry["outcome"] = "blocked"
            st["history"].append(entry)
            return self.transition(st, "blocked", text.strip().splitlines()[0][:200] if text.strip() else "BLOCKED.md")

        git("add", "-A", cwd=wt)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wt).returncode != 0:
            try:
                git("commit", "-q", "-m", f"fleet({st['id']}): attempt {st['attempts']} uncommitted changes", cwd=wt)
            except RuntimeError as e:  # e.g. a pre-commit hook; the gate still judges the worktree
                reason = (reason + "; " if reason else "") + f"auto-commit failed: {str(e)[:200]}"
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wt).returncode != 0
        commits = int(git("rev-list", "--count", f"{st['base_sha']}..HEAD", cwd=wt) or 0)
        if commits == 0 and not staged:
            gate_rc, gate_out = 1, "no changes on the branch"
        else:
            gate_rc, gate_out = self.run_gate(self.opt(meta, "accept"), wt, float(self.opt(meta, "gate_timeout_min")))
        (run / "gate.log").write_text(gate_out, encoding="utf-8")
        entry.update({"gate_rc": gate_rc, "commits": commits, "note": reason})
        st["history"].append(entry)
        st["head"] = git("rev-parse", "--short", "HEAD", cwd=wt)

        if gate_rc == 0:
            entry["outcome"] = "passed"
            hook = meta.get("on_pass", self.cfg.get("on_pass", ""))
            if hook:
                hook = hook.replace("{id}", st["id"]).replace("{branch}", st["branch"])
                hook_rc, hook_out = self.run_gate(hook, wt, 5)
                (run / "on_pass.log").write_text(hook_out, encoding="utf-8")
                if hook_rc != 0:
                    entry["outcome"] = "passed_hook_failed"
                    return self.transition(st, "review", f"gate passed but on_pass failed (rc={hook_rc}): "
                                                         f"{tail(hook_out, 200).strip()}")
            if str(self.opt(meta, "risk")) == "low":
                return self.transition(st, "done", f"gate passed at {st['head']} ({commits} commits)")
            return self.transition(st, "review", f"gate passed, risk={self.opt(meta, 'risk')}: human review")
        entry["outcome"] = "gate_failed"
        why = reason or f"acceptance failed (rc={gate_rc})"
        if st["attempts"] < int(self.opt(meta, "max_attempts")):
            st["feedback"].append(f"Attempt {st['attempts']} on {st['runtime']} did not pass: {why}\n"
                                  f"Acceptance command output (tail):\n```\n{tail(gate_out, 2000)}\n```")
            return self.transition(st, "queued", f"retry: {why}")
        return self.transition(st, "failed", f"{why}; attempts exhausted ({st['attempts']})")

    def waiting_on(self, meta):
        deps = meta.get("depends_on") or []
        deps = deps if isinstance(deps, list) else [deps]
        waiting = [d for d in deps if self.load_state(d).get("status") != "done"]
        return waiting

    def dispatch(self, cards):
        caps = self.cfg.get("caps", {})
        states = {cid: self.load_state(cid) for cid in cards}
        running = {}
        for st in states.values():
            if st["status"] == "running":
                running[st["runtime"]] = running.get(st["runtime"], 0) + 1
        queue = sorted((st for st in states.values() if st["status"] == "queued"),
                       key=lambda s: (int(self.opt(cards[s["id"]][0], "priority")), s["created_at"]))
        for st in queue:
            meta, body = cards[st["id"]]
            if self.waiting_on(meta):
                continue
            rt = meta.get("fallback") if st["attempts"] >= 1 and meta.get("fallback") else self.opt(meta, "runtime")
            if running.get(rt, 0) >= int(caps.get(rt, 1)):
                continue
            try:
                self.launch(st, meta, body, rt)
                running[rt] = running.get(rt, 0) + 1
            except (RuntimeError, OSError, KeyError, ValueError) as e:
                self.transition(st, "failed", f"dispatch error: {e}")

    def launch(self, st, meta, body, rt):
        cid = st["id"]
        if not ID_RE.match(cid):
            raise ValueError(f"invalid id {cid!r}")
        if not meta.get("accept"):
            raise ValueError("card has no accept command")
        repo = Path(os.path.expanduser(meta.get("repo", ""))).resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"repo {repo} is not a git repository")
        rt_cfg = self.cfg["runtimes"][rt]
        wt, branch = self.wt_dir / cid, f"fleet/{cid}"
        if not wt.exists():
            base = self.opt(meta, "base")
            st["base_sha"] = git("rev-parse", base, cwd=repo)
            exists = subprocess.run(["git", "rev-parse", "--verify", "-q", branch], cwd=repo,
                                    capture_output=True).returncode == 0
            git("worktree", "add", "-q", *([] if exists else ["-b", branch]), str(wt),
                *([branch] if exists else [st["base_sha"]]), cwd=repo)
        gitdir = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=wt)

        st["attempts"] += 1
        # a fresh run dir per launch (attempts reset on requeue; a reused dir would hold a stale exit_code)
        run_root = self.runs_dir / cid
        used = [int(p.name) for p in run_root.glob("*") if p.name.isdigit()] if run_root.exists() else []
        run = run_root / str(max(used, default=0) + 1)
        run.mkdir(parents=True)
        prompt = body + CONTRACT.format(worktree=wt, branch=branch, accept=self.opt(meta, "accept"))
        if st["feedback"]:
            prompt += ("\n## Feedback from previous attempts (fix these; the branch already contains the "
                       "earlier commits)\n\n" + "\n\n".join(st["feedback"][-3:]) + "\n")
        (run / "prompt.txt").write_text(prompt, encoding="utf-8")

        subs = {"{model}": str(meta.get("model") or rt_cfg.get("model", "")), "{worktree}": str(wt),
                "{gitdir}": gitdir, "{run}": str(run)}
        argv = []
        for a in rt_cfg["argv"]:
            for k, v in subs.items():
                a = a.replace(k, v)
            argv.append(a)
        q = shlex.quote
        wrapper = (f"cd {q(str(wt))} && {' '.join(q(a) for a in argv)} < {q(str(run / 'prompt.txt'))} "
                   f"> {q(str(run / 'stdout.txt'))} 2> {q(str(run / 'stderr.txt'))}; "
                   f"echo $? > {q(str(run / 'exit_code'))}")
        proc = subprocess.Popen(["/bin/sh", "-c", wrapper], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        st.update({"pid": proc.pid, "runtime": rt, "run_dir": str(run), "worktree": str(wt),
                   "branch": branch, "started_at": now()})
        self.transition(st, "running", f"attempt {st['attempts']} on {rt}")

    def report(self, cards):
        states = [self.load_state(cid) for cid in cards]
        by = {}
        for st in states:
            by.setdefault(st["status"], []).append(st)
        caps = self.cfg.get("caps", {})
        run_by_rt = {}
        for st in by.get("running", []):
            run_by_rt[st["runtime"]] = run_by_rt.get(st["runtime"], 0) + 1
        lines = [f"# Fleet status - {iso(now())}", "",
                 "running " + ", ".join(f"{rt} {run_by_rt.get(rt, 0)}/{cap}" for rt, cap in caps.items())
                 + " | " + " | ".join(f"{s} {len(by.get(s, []))}" for s in
                                      ("queued", "review", "blocked", "failed", "done")), ""]
        lines.append("## Needs you")
        for s in HUMAN_STATES:
            for st in by.get(s, []):
                lines.append(f"- [{s}] {st['id']} - {st.get('reason', '')} - worktree {st.get('worktree', '-')}")
        if not any(by.get(s) for s in HUMAN_STATES):
            lines.append("- nothing")
        lines += ["", "## Running"]
        for st in by.get("running", []):
            lines.append(f"- {st['id']} - {st['runtime']} attempt {st['attempts']} - "
                         f"{(now() - st['started_at']) / 60:.0f} min")
        lines += ["", "## Queued"]
        for st in by.get("queued", []):
            meta = cards[st["id"]][0]
            waiting = self.waiting_on(meta)
            note = f" (waiting on {', '.join(waiting)})" if waiting else ""
            note += f" (retry #{st['attempts'] + 1})" if st["attempts"] else ""
            lines.append(f"- {st['id']} - {meta.get('runtime', self.defaults['runtime'])}{note}")
        lines += ["", "## Done"]
        for st in sorted(by.get("done", []), key=lambda s: -s.get("updated_at", 0)):
            lines.append(f"- {st['id']} - {st.get('branch')} @ {st.get('head')} - {st.get('reason', '')}")
        if self.problems:
            lines += ["", "## Card problems"] + [f"- {p}" for p in self.problems]
        write_atomic(self.home / "STATUS.md", "\n".join(lines) + "\n")

    def tick(self):
        lock = self.home / ".tick.lock"
        try:
            lock.mkdir()
        except FileExistsError:
            try:
                fresh = now() - lock.stat().st_mtime < 15 * 60
            except FileNotFoundError:
                fresh = False
            if fresh:
                print("another tick is running; skipped")
                return
            try:
                lock.rmdir()
                lock.mkdir()
            except (FileNotFoundError, FileExistsError):
                print("lock contention; skipped")
                return
        try:
            cards = self.load_cards()
            self.monitor(cards)
            self.dispatch(cards)
            self.report(cards)
        finally:
            lock.rmdir()


def cmd_init(home):
    home = Path(home)
    for d in ("cards", "state", "runs", "worktrees"):
        (home / d).mkdir(parents=True, exist_ok=True)
    cfg = home / "config.json"
    if not cfg.exists():
        cfg.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    example = home / "cards" / "example-add-mul.md.sample"
    example.write_text(EXAMPLE_CARD, encoding="utf-8")
    print(f"initialised {home.resolve()} (edit config.json; copy {example.name} to <id>.md to queue a card)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default="fleet")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("tick")
    w = sub.add_parser("watch")
    w.add_argument("interval", type=int, nargs="?", default=300)
    sub.add_parser("status")
    r = sub.add_parser("requeue")
    r.add_argument("id")
    r.add_argument("--note", default="")
    s = sub.add_parser("stop")
    s.add_argument("id")
    args = ap.parse_args()

    if args.cmd == "init":
        return cmd_init(args.home)
    if not (Path(args.home) / "cards").is_dir():
        sys.exit(f"{args.home} is not a fleet home; run: fleet.py --home {args.home} init")
    fleet = Fleet(args.home)
    if args.cmd == "tick":
        fleet.tick()
    elif args.cmd == "watch":
        if args.interval < 10:
            sys.exit("interval must be >= 10 seconds")
        while True:
            try:
                Fleet(args.home).tick()
            except Exception as e:  # keep patrolling; the next tick retries
                print(f"[{iso(now())}] tick error: {e}", file=sys.stderr)
            time.sleep(args.interval)
    elif args.cmd == "status":
        fleet.report(fleet.load_cards())
        print((fleet.home / "STATUS.md").read_text(encoding="utf-8"))
    elif args.cmd in ("requeue", "stop"):
        if not (fleet.state_dir / f"{args.id}.json").exists():
            sys.exit(f"unknown card {args.id}")
        st = fleet.load_state(args.id)
        if args.cmd == "stop":
            if st["status"] == "running" and fleet.owned(st["pid"], st["run_dir"]):
                fleet.kill_group(st["pid"])
            fleet.transition(st, "failed", "stopped by user")
        else:
            if st["status"] == "running":
                sys.exit(f"{args.id} is running; stop it first")
            if args.note:
                st["feedback"].append(f"Note from the human reviewer:\n{args.note}")
            st["attempts"] = 0
            fleet.transition(st, "queued", "requeued by user" + (f": {args.note[:100]}" if args.note else ""))


if __name__ == "__main__":
    main()
