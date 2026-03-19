# Claude Code Monitor

Lightweight, zero-dependency usage monitor for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Reads local session data directly from `~/.claude/projects/` — no database, no proxy, no API keys needed.

## Features

- **Real-time cost tracking** — per-session and all-time cost based on Anthropic's official pricing
- **Token breakdown** — input, output, cache read, cache write
- **Context window monitor** — see how full your context is (know when to `/compact`)
- **Rate limit countdown** — progress bar showing time until next 5h window reset
- **Model distribution** — usage breakdown across Opus / Sonnet / Haiku
- **Session history** — all sessions with per-model stats and timelines

## Components

### 1. Status Line (in Claude Code)

A 4-line status bar that lives inside your Claude Code terminal:

```
◇ Opus │ Session $5.67  Total $841.80

Limit   ▄▄▄▄▁▁▁▁▁▁ 42% Reset 2h51m -> 15:00

Context ▄▄▄▄▄▄▄▁▁▁ 70% 140.0K/200.0K │ I 125.0K O 15.0K CR 80.0K CW 35.0K

Models  Opus ▄▄▄▄▁▁▁▁ 51% 319M  Sonnet ▄▄▄▁▁▁▁▁ 44% 274M  Haiku ▁▁▁▁▁▁▁▁ 3% 24M
```

**Setup:**

```bash
# Copy the status line script
cp statusline.sh ~/.claude/statusline.sh
chmod +x ~/.claude/statusline.sh

# Add to your Claude Code settings (~/.claude/settings.json)
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh"
  }
}
```

Restart Claude Code and the status line appears automatically.

### 2. Web Dashboard (`dashboard.py`)

A self-contained web dashboard served on `localhost:8199`.

```bash
python3 dashboard.py
# Open http://localhost:8199
```

Features:
- Summary metric cards (cost, tokens, sessions, today's usage)
- Daily usage timeline chart (cost / tokens / requests)
- Model distribution doughnut chart
- Per-model breakdown cards
- Session history table
- 30-second auto-refresh

Options:
```
--port PORT        Server port (default: 8199)
--claude-dir DIR   Claude data directory (default: ~/.claude)
```

### 3. Terminal TUI (`tui.py`)

A full-screen terminal dashboard for split-pane monitoring (tmux / iTerm2).

```bash
python3 tui.py

# Or in a tmux split pane
tmux split-window -h "python3 ~/claude-code-monitor/tui.py"
```

Features:
- Overview: total cost, sessions, messages, today vs. yesterday
- Token breakdown: input/output/cache with bar charts
- Model ranking with usage bars
- Daily usage timeline (last 10 days)
- Session list with per-session details
- 5-second auto-refresh, press `r` to refresh, `q` to quit

Options:
```
--interval N       Refresh interval in seconds (default: 5)
--claude-dir DIR   Claude data directory (default: ~/.claude)
```

## How It Works

Claude Code stores conversation data as JSONL files in `~/.claude/projects/`. Each assistant message includes a `usage` object with token counts:

```json
{
  "type": "assistant",
  "message": {
    "model": "claude-opus-4-6",
    "usage": {
      "input_tokens": 1500,
      "output_tokens": 800,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 3000
    }
  }
}
```

This tool parses all session JSONL files (including subagent files) and calculates costs using Anthropic's published pricing:

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|-------------|------------|
| Opus 4.6 | $15/M | $75/M | $18.75/M | $1.50/M |
| Sonnet 4.6 | $3/M | $15/M | $3.75/M | $0.30/M |
| Haiku 4.5 | $0.80/M | $4/M | $1/M | $0.08/M |

## Requirements

- Python 3.8+
- No external dependencies (stdlib only)
- Claude Code installed with session data in `~/.claude/`

## License

MIT
