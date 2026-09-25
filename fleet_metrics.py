#!/usr/bin/env python3
"""Measure how much agent work one person actually drives with Claude Code.

Reads only local Claude Code data (read-only):
  ~/.claude/history.jsonl            prompts you typed
  ~/.claude/projects/**/*.jsonl      session transcripts (main + subagents)

Usage:  python3 fleet_metrics.py [--days 7] [--tz 8]   (--tz defaults to machine local time)

Key numbers to track week over week:
  agent-hours/day        total time agents were actually working
  turn length p50        how long an agent runs per human touch (autonomy)
  concurrency p50/p90    how many main sessions work at the same minute
  $/agent-hour           API-list-price equivalent of the token usage
"""
import argparse
import bisect
import collections
import datetime
import glob
import json
import os
import re
import sys
import time

# $/MTok list prices (input, output, cache_read). Cache writes: 5m = 1.25x input, 1h = 2x input.
# Source: claude-api skill model table cached 2026-06-24 -- confirm on the pricing page before quoting.
PRICES = {
    "fable-5-1": (10, 50, 0.25), "fable-5": (10, 50, 1.0),
    "opus-5-5": (4, 20, 0.20), "opus-5": (5, 25, 0.50),
    "opus-4-8": (5, 25, 0.50), "opus-4-7": (5, 25, 0.50), "opus-4-6": (5, 25, 0.50),
    "sonnet-5": (2, 10, 0.20), "sonnet-4-6": (3, 15, 0.30),
    "haiku-4-5": (1, 5, 0.10),
}
WORK_GAP_CAP = 20 * 60  # a single gap longer than this is not counted as continuous work
POLL_RE = re.compile(r"^\s*(怎么样了|进度怎么样了?|进展|状态|部署了吗|测试通过了吗|好了吗|完成了吗|现在呢|status)\s*[?？。.!！]*\s*$", re.I)
CMD_PREFIXES = ("<command-", "<local-command", "<task-notification", "<system-reminder")


def pct(values, p):
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def norm_model(m):
    m = (m or "").removeprefix("claude-")
    return re.sub(r"-\d{8}$", "", m)


def is_human_prompt(d):
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return False
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        return not c.lstrip().startswith(CMD_PREFIXES)
    if isinstance(c, list):
        blocks = [b for b in c if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return False
        return any(b.get("type") in ("text", "image") for b in blocks)
    return False


def prompt_stats(since, tz, ndays):
    path = os.path.expanduser("~/.claude/history.jsonl")
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ts = (d.get("timestamp") or 0) / 1000
                if ts >= since:
                    rows.append((ts, (d.get("display") or "").strip(), d.get("sessionId") or ""))
    except OSError as e:
        print(f"[prompts] cannot read {path}: {e}")
        return
    rows.sort()
    if not rows:
        print("[prompts] no prompts in window")
        return
    active_days = len({datetime.datetime.fromtimestamp(r[0], tz).date() for r in rows})
    typed = [r for r in rows if not r[1].startswith("/")]
    ts = [r[0] for r in rows]
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a < 1800]
    touched = []
    for i, r in enumerate(rows):
        j = bisect.bisect_left(ts, r[0] - 3600)
        touched.append(len({x[2] for x in rows[j:i + 1]}))
    polls = sum(1 for r in typed if POLL_RE.match(r[1]))
    print(f"prompts typed/day        {len(rows) / ndays:.0f}  (calendar days touched: {active_days})")
    print(f"short prompts (<=15ch)   {sum(len(r[1]) <= 15 for r in typed) / max(1, len(typed)):.0%}   status polls: {polls}")
    print(f"gap between prompts      p50 {pct(gaps, .5):.0f}s  p75 {pct(gaps, .75):.0f}s")
    print(f"sessions touched / hour  p50 {pct(touched, .5)}  p90 {pct(touched, .9)}  max {max(touched)}")


def transcript_stats(since, tz, ndays):
    root = os.path.expanduser("~/.claude/projects")
    files = [f for f in glob.glob(root + "/**/*.jsonl", recursive=True) if os.path.getmtime(f) >= since]
    work = collections.Counter()
    subwork = collections.Counter()
    turns, waits = [], []
    active_minutes = collections.defaultdict(set)
    seen_msgs = set()
    usage = collections.defaultdict(collections.Counter)
    for f in files:
        sub = "/subagents/" in f
        events = []
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"timestamp"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") not in ("user", "assistant"):
                        continue
                    ts = parse_ts(d.get("timestamp"))
                    if ts is None or ts < since:
                        continue
                    events.append((ts, (not sub) and is_human_prompt(d)))
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    mid = msg.get("id") or d.get("uuid")
                    if d.get("type") == "assistant" and u and mid not in seen_msgs:
                        seen_msgs.add(mid)
                        c = usage[norm_model(msg.get("model"))]
                        c["in"] += u.get("input_tokens") or 0
                        c["out"] += u.get("output_tokens") or 0
                        c["cr"] += u.get("cache_read_input_tokens") or 0
                        split = u.get("cache_creation") or {}
                        w1h = split.get("ephemeral_1h_input_tokens") or 0
                        c["cw1h"] += w1h
                        c["cw5m"] += max(0, (u.get("cache_creation_input_tokens") or 0) - w1h)
        except OSError:
            continue
        events.sort()
        turn_start = None
        for (a, _), (b, b_human) in zip(events, events[1:]):
            gap = b - a
            if b_human:
                if 0 <= gap < 12 * 3600:
                    waits.append(gap)
                if turn_start is not None:
                    turns.append(a - turn_start)
                turn_start = b
                continue
            w = min(max(gap, 0), WORK_GAP_CAP)
            day = datetime.datetime.fromtimestamp(b, tz).date()
            (subwork if sub else work)[day] += w
            if not sub:
                for m in range(int(a // 60), int((a + w) // 60) + 1):
                    active_minutes[m].add(f)
        if turn_start is not None and events:
            turns.append(events[-1][0] - turn_start)

    total_h = (sum(work.values()) + sum(subwork.values())) / 3600
    conc = [len(v) for v in active_minutes.values()]
    print(f"transcripts scanned      {len(files)}")
    print(f"agent-hours/day          {total_h / ndays:.1f}  (main {sum(work.values()) / 3600 / ndays:.1f}, subagents {sum(subwork.values()) / 3600 / ndays:.1f})")
    print(f"agent run per touch      p50 {pct(turns, .5) / 60:.1f}min  p75 {pct(turns, .75) / 60:.1f}min  p90 {pct(turns, .9) / 60:.1f}min")
    print(f"your reply latency       p50 {pct(waits, .5) / 60:.1f}min  p75 {pct(waits, .75) / 60:.1f}min")
    print(f"concurrent main sessions p50 {pct(conc, .5)}  p90 {pct(conc, .9)}  max {max(conc) if conc else 0}   "
          f"minutes with any agent working: {len(conc) / (ndays * 1440):.0%}")

    cost, unknown = 0.0, []
    for model, c in usage.items():
        p = PRICES.get(model)
        if not p:
            if sum(c.values()):
                unknown.append(model)
            continue
        pin, pout, pcr = p
        cost += (c["in"] * pin + c["cw5m"] * pin * 1.25 + c["cw1h"] * pin * 2 + c["cr"] * pcr + c["out"] * pout) / 1e6
    tokens = sum(sum(c.values()) for c in usage.values())
    cache_reads = sum(c["cr"] for c in usage.values())
    print(f"tokens                   {tokens / 1e6:,.0f}M total, cache reads {cache_reads / max(1, tokens):.0%}")
    if total_h:
        print(f"API-list-price equiv.    ${cost:,.0f} in window  ->  ${cost / ndays:,.0f}/day, ${cost / total_h:.2f}/agent-hour")
    if unknown:
        print(f"(no price for: {', '.join(sorted(unknown))} -- excluded from cost)")
    by_model = sorted(((m, sum(c.values())) for m, c in usage.items()), key=lambda x: -x[1])
    print("token share by model     " + ", ".join(f"{m} {t / max(1, tokens):.0%}" for m, t in by_model if t))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tz", type=float, default=None, help="UTC offset in hours for day buckets (default: machine local time)")
    args = ap.parse_args()
    if args.days < 1:
        sys.exit("--days must be >= 1")
    tz = datetime.timezone(datetime.timedelta(hours=args.tz)) if args.tz is not None else None
    since = time.time() - args.days * 86400
    print(f"== window: last {args.days} days ==")
    prompt_stats(since, tz, args.days)
    transcript_stats(since, tz, args.days)


if __name__ == "__main__":
    main()
