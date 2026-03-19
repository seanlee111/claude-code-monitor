#!/usr/bin/env python3
"""
Claude Code Monitor — Terminal TUI
Real-time monitoring dashboard for Claude Code usage.
Runs in a terminal (split pane / tmux / iTerm) and auto-refreshes.

Usage:
    python3 ~/claude-code-monitor/tui.py
    python3 ~/claude-code-monitor/tui.py --interval 5  # refresh every 5s
"""

import os
import sys
import glob
import json
import time
import argparse
import shutil
from datetime import datetime
from collections import defaultdict
from pathlib import Path
import re

# ── ANSI helpers ─────────────────────────────────────────────────────────────
ESC = "\033"
RESET   = f"{ESC}[0m"
BOLD    = f"{ESC}[1m"
DIM     = f"{ESC}[2m"
GREEN   = f"{ESC}[32m"
BGREEN  = f"{ESC}[92m"
BLUE    = f"{ESC}[34m"
BBLUE   = f"{ESC}[94m"
CYAN    = f"{ESC}[36m"
BCYAN   = f"{ESC}[96m"
YELLOW  = f"{ESC}[33m"
BYELLOW = f"{ESC}[93m"
RED     = f"{ESC}[31m"
BRED    = f"{ESC}[91m"
MAGENTA = f"{ESC}[35m"
BMAGENTA= f"{ESC}[95m"
WHITE   = f"{ESC}[37m"
BWHITE  = f"{ESC}[97m"
GRAY    = f"{ESC}[90m"
CLEAR   = f"{ESC}[2J{ESC}[H"
HIDE_CURSOR  = f"{ESC}[?25l"
SHOW_CURSOR  = f"{ESC}[?25h"

def goto(row, col=1):
    return f"{ESC}[{row};{col}H"

def clrline():
    return f"{ESC}[2K"

# ── Model pricing ─────────────────────────────────────────────────────────────
PRICING = {
    "opus":   {"input": 15.0,  "output": 75.0,  "cw": 18.75, "cr": 1.50},
    "sonnet": {"input": 3.0,   "output": 15.0,  "cw": 3.75,  "cr": 0.30},
    "haiku":  {"input": 0.80,  "output": 4.0,   "cw": 1.0,   "cr": 0.08},
}

def get_price(model: str) -> dict:
    for k, v in PRICING.items():
        if k in (model or ""):
            return v
    return PRICING["sonnet"]

def calc_cost(usage: dict, model: str) -> float:
    p = get_price(model)
    return (
        usage.get("input_tokens", 0) / 1e6 * p["input"]
        + usage.get("output_tokens", 0) / 1e6 * p["output"]
        + usage.get("cache_creation_input_tokens", 0) / 1e6 * p["cw"]
        + usage.get("cache_read_input_tokens", 0) / 1e6 * p["cr"]
    )

def parse_ts(ts):
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None

# ── Data loading ──────────────────────────────────────────────────────────────
def load_data(claude_dir: str) -> dict:
    projects_dir = os.path.join(claude_dir, "projects")
    if not os.path.exists(projects_dir):
        return {}

    # Find all JSONL files (main sessions + subagents)
    jsonl_files = glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))
    jsonl_files += glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl"))

    # Group files by session
    session_files: dict[tuple, list] = defaultdict(list)
    for f in jsonl_files:
        parts = Path(f).parts
        for i, part in enumerate(parts):
            bare = part.replace(".jsonl", "")
            if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", bare):
                proj_idx = list(parts).index("projects") + 1
                project = parts[proj_idx] if proj_idx < len(parts) else "unknown"
                session_files[(bare, project)].append(f)
                break

    sessions = []
    all_daily = defaultdict(lambda: {"cost": 0.0, "input": 0, "output": 0, "count": 0})
    model_totals: dict = defaultdict(lambda: {"input": 0, "output": 0, "cache": 0, "cost": 0.0, "count": 0})
    grand_cost = 0.0
    grand_input = 0
    grand_output = 0
    grand_cache_write = 0
    grand_cache_read = 0
    grand_count = 0

    for (session_id, project), files in session_files.items():
        sd = {
            "id": session_id,
            "project": project.replace("-Users-bytedance-", "").replace("-Users-bytedance", "~"),
            "cost": 0.0,
            "input": 0,
            "output": 0,
            "cache_write": 0,
            "cache_read": 0,
            "count": 0,
            "models": set(),
            "start": None,
            "end": None,
        }

        for filepath in files:
            try:
                with open(filepath) as fh:
                    for line in fh:
                        try:
                            d = json.loads(line.strip())
                        except json.JSONDecodeError:
                            continue
                        if d.get("type") != "assistant":
                            continue
                        msg = d.get("message", {})
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        model = msg.get("model", "unknown")
                        if model == "<synthetic>":
                            continue

                        cost = calc_cost(usage, model)
                        inp = usage.get("input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        cw  = usage.get("cache_creation_input_tokens", 0)
                        cr  = usage.get("cache_read_input_tokens", 0)

                        sd["cost"] += cost
                        sd["input"] += inp
                        sd["output"] += out
                        sd["cache_write"] += cw
                        sd["cache_read"] += cr
                        sd["count"] += 1
                        sd["models"].add(model)

                        dt = parse_ts(d.get("timestamp"))
                        if dt:
                            if sd["start"] is None or dt < sd["start"]:
                                sd["start"] = dt
                            if sd["end"] is None or dt > sd["end"]:
                                sd["end"] = dt
                            day_key = dt.strftime("%Y-%m-%d")
                            all_daily[day_key]["cost"] += cost
                            all_daily[day_key]["input"] += inp
                            all_daily[day_key]["output"] += out
                            all_daily[day_key]["count"] += 1

                        mt = model_totals[model]
                        mt["input"] += inp
                        mt["output"] += out
                        mt["cache"] += cw + cr
                        mt["cost"] += cost
                        mt["count"] += 1

                        grand_cost += cost
                        grand_input += inp
                        grand_output += out
                        grand_cache_write += cw
                        grand_cache_read += cr
                        grand_count += 1
            except (OSError, IOError):
                continue

        if sd["count"] > 0:
            sd["models"] = sorted(sd["models"])
            sessions.append(sd)

    sessions.sort(key=lambda s: s["end"] or datetime.min, reverse=True)

    today = datetime.now().strftime("%Y-%m-%d")
    today_data = all_daily.get(today, {"cost": 0.0, "input": 0, "output": 0, "count": 0})
    yesterday = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).replace(
        day=datetime.now().day - 1) if datetime.now().day > 1 else None)
    yesterday_key = yesterday.strftime("%Y-%m-%d") if yesterday else ""
    yesterday_data = all_daily.get(yesterday_key, {"cost": 0.0, "count": 0})

    return {
        "sessions": sessions,
        "grand": {
            "cost": grand_cost,
            "input": grand_input,
            "output": grand_output,
            "cache_write": grand_cache_write,
            "cache_read": grand_cache_read,
            "count": grand_count,
            "session_count": len(sessions),
        },
        "today": today_data,
        "yesterday": yesterday_data,
        "model_totals": dict(model_totals),
        "daily": dict(sorted(all_daily.items())),
    }

# ── Formatting helpers ────────────────────────────────────────────────────────
def fmt_tok(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(v: float) -> str:
    if v >= 100:
        return f"${v:.0f}"
    if v >= 1:
        return f"${v:.2f}"
    if v >= 0.01:
        return f"${v:.3f}"
    return f"${v:.4f}"

def bar(val: float, max_val: float, width: int = 12, color: str = BBLUE) -> str:
    if max_val <= 0:
        return GRAY + "─" * width + RESET
    filled = min(int(val / max_val * width), width)
    empty = width - filled
    return color + "█" * filled + GRAY + "░" * empty + RESET

def model_tag(model: str) -> str:
    if "opus" in model:
        return BRED + "Opus" + RESET
    if "sonnet" in model:
        return BMAGENTA + "Sonnet" + RESET
    if "haiku" in model:
        return BCYAN + "Haiku" + RESET
    return GRAY + model[:10] + RESET

def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n-1] + "…"

# ── Rendering ─────────────────────────────────────────────────────────────────
def render(data: dict, width: int, height: int, last_refresh: datetime) -> list[str]:
    lines: list[str] = []
    W = width

    def line(content: str = "", fill: bool = False):
        if fill:
            lines.append(f"{content}")
        else:
            lines.append(content)

    def divider(char="─", color=GRAY):
        lines.append(f"{color}{char * W}{RESET}")

    def header_line(title: str):
        pad_left = (W - len(title) - 4) // 2
        pad_right = W - len(title) - 4 - pad_left
        lines.append(
            f"{GRAY}{'─' * pad_left}{RESET}"
            f" {BOLD}{BYELLOW}{title}{RESET} "
            f"{GRAY}{'─' * pad_right}{RESET}"
        )

    g = data.get("grand", {})
    today = data.get("today", {})
    yesterday = data.get("yesterday", {})
    models = data.get("model_totals", {})
    sessions = data.get("sessions", [])

    # ── Top Header ────────────────────────────────────────────────────────────
    now_str = last_refresh.strftime("%H:%M:%S")
    title = f"  ◇ Claude Code Monitor   {GRAY}Updated {now_str}"
    line(f"{BOLD}{BWHITE}{title}{RESET}")
    divider("═", GRAY)

    # ── Summary Metrics ───────────────────────────────────────────────────────
    header_line("OVERVIEW")

    # Row 1: Total cost, sessions, messages
    total_cost_str = f"{BGREEN}{BOLD}{fmt_cost(g.get('cost',0))}{RESET}"
    sessions_str   = f"{BBLUE}{BOLD}{g.get('session_count',0)}{RESET}"
    msgs_str       = f"{BCYAN}{BOLD}{fmt_tok(g.get('count',0))}{RESET}"

    today_cost_str = f"{BGREEN}{fmt_cost(today.get('cost',0))}{RESET}"
    today_msgs_str = f"{BCYAN}{fmt_tok(today.get('count',0))}{RESET}"
    yest_cost = yesterday.get('cost', 0)
    yest_str   = f"{GRAY}{fmt_cost(yest_cost)}{RESET}"

    line(f"  {GRAY}Total Cost{RESET}       {total_cost_str:>30}   {GRAY}Today{RESET}  {today_cost_str}")
    line(f"  {GRAY}Sessions{RESET}         {sessions_str:>30}   {GRAY}Yester{RESET} {yest_str}")
    line(f"  {GRAY}Messages{RESET}         {msgs_str:>30}   {GRAY}Today msgs{RESET} {today_msgs_str}")

    # Token breakdown
    inp_str  = f"{BBLUE}{fmt_tok(g.get('input',0))}{RESET}"
    out_str  = f"{BBLUE}{fmt_tok(g.get('output',0))}{RESET}"
    cw_str   = f"{BYELLOW}{fmt_tok(g.get('cache_write',0))}{RESET}"
    cr_str   = f"{BYELLOW}{fmt_tok(g.get('cache_read',0))}{RESET}"
    line(f"  {GRAY}Tokens I/O{RESET}       {inp_str} {GRAY}/{RESET} {out_str}   {GRAY}Cache W/R{RESET} {cw_str} {GRAY}/{RESET} {cr_str}")

    # ── Model Breakdown ───────────────────────────────────────────────────────
    header_line("MODELS")

    max_model_cost = max((v["cost"] for v in models.values()), default=1)
    for mname, mdata in sorted(models.items(), key=lambda x: -x[1]["cost"]):
        tag = model_tag(mname)
        b = bar(mdata["cost"], max_model_cost, 14)
        cost_s = f"{BGREEN}{fmt_cost(mdata['cost'])}{RESET}"
        tok_s  = f"{GRAY}in:{fmt_tok(mdata['input'])} out:{fmt_tok(mdata['output'])} cache:{fmt_tok(mdata['cache'])}{RESET}"
        cnt_s  = f"{GRAY}{mdata['count']} req{RESET}"
        line(f"  {tag:>20} {b} {cost_s:>12}  {tok_s}  {cnt_s}")

    # ── Daily Timeline ────────────────────────────────────────────────────────
    daily = data.get("daily", {})
    header_line("DAILY USAGE")

    max_day_cost = max((v["cost"] for v in daily.values()), default=1)
    for day, ddata in sorted(daily.items(), reverse=True)[:10]:
        b = bar(ddata["cost"], max_day_cost, 16, BGREEN)
        cost_s = f"{BGREEN}{fmt_cost(ddata['cost']):<8}{RESET}"
        day_disp = datetime.strptime(day, "%Y-%m-%d").strftime("%m/%d")
        today_mark = f"{BYELLOW} ◀ today{RESET}" if day == datetime.now().strftime("%Y-%m-%d") else ""
        cnt_s = f"{GRAY}{ddata['count']:>4} req{RESET}"
        line(f"  {GRAY}{day_disp}{RESET}  {b}  {cost_s}  {cnt_s}{today_mark}")

    # ── Recent Sessions ───────────────────────────────────────────────────────
    header_line("SESSIONS")

    # Column widths
    col_id  = 8
    col_proj = min(22, W // 5)
    col_mod  = 8
    col_msg  = 5
    col_cost = 8
    col_tok  = 16

    hdr = (
        f"  {GRAY}{'ID':>{col_id}}  {'Project':<{col_proj}}  {'Model':<{col_mod}}"
        f"  {'Req':>{col_msg}}  {'Cost':>{col_cost}}  {'In/Out Tokens':<{col_tok}}  {'Time'}{RESET}"
    )
    line(hdr)

    for s in sessions[:min(len(sessions), height - len(lines) - 4)]:
        sid  = s["id"][:col_id]
        proj = trunc(s["project"], col_proj)
        mods = "/".join(model_tag(m) for m in s["models"])
        cnt  = str(s["count"])
        cost = f"{BGREEN}{fmt_cost(s['cost'])}{RESET}"
        toks = f"{GRAY}{fmt_tok(s['input'])}/{fmt_tok(s['output'])}{RESET}"
        time_str = s["end"].strftime("%m/%d %H:%M") if s["end"] else "─"
        line(
            f"  {GRAY}{sid}{RESET}  "
            f"{BCYAN}{proj:<{col_proj}}{RESET}  "
            f"{mods:<{col_mod+20}}  "
            f"{cnt:>{col_msg}}  "
            f"{cost:>{col_cost+12}}  "
            f"{toks}  "
            f"{GRAY}{time_str}{RESET}"
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    divider("─", GRAY)
    line(f"  {GRAY}q{RESET} quit  {GRAY}r{RESET} refresh now  {GRAY}Auto-refresh: every {INTERVAL}s{RESET}")

    return lines


# ── Main loop ─────────────────────────────────────────────────────────────────
INTERVAL = 5  # seconds

def run(claude_dir: str, interval: int):
    global INTERVAL
    INTERVAL = interval

    print(HIDE_CURSOR, end="", flush=True)

    try:
        import select
        import tty
        import termios

        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

        while True:
            W, H = shutil.get_terminal_size((120, 40))
            data = load_data(claude_dir)
            now = datetime.now()

            output = [CLEAR]
            rendered = render(data, W, H, now)
            for i, l in enumerate(rendered[:H]):
                output.append(l)
            sys.stdout.write("\n".join(output))
            sys.stdout.flush()

            # Wait for keypress or timeout
            deadline = time.time() + interval
            while time.time() < deadline:
                remaining = deadline - time.time()
                ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 0.2))
                if ready:
                    ch = sys.stdin.read(1)
                    if ch in ("q", "Q", "\x03"):  # q or Ctrl-C
                        return
                    if ch in ("r", "R"):
                        break  # refresh immediately

    except (ImportError, termios.error):
        # Fallback: simple mode without keypress detection
        try:
            while True:
                W, H = shutil.get_terminal_size((120, 40))
                data = load_data(claude_dir)
                now = datetime.now()
                output = [CLEAR]
                rendered = render(data, W, H, now)
                for l in rendered[:H]:
                    output.append(l)
                sys.stdout.write("\n".join(output))
                sys.stdout.flush()
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
    finally:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        except Exception:
            pass
        print(SHOW_CURSOR, end="", flush=True)
        print()


def main():
    parser = argparse.ArgumentParser(description="Claude Code Terminal Monitor")
    parser.add_argument("--interval", "-i", type=int, default=5, help="Refresh interval in seconds")
    parser.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"), help="Claude data directory")
    args = parser.parse_args()

    run(args.claude_dir, args.interval)


if __name__ == "__main__":
    main()
