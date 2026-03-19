#!/usr/bin/env python3
"""
Claude Code Usage Monitor Dashboard
A lightweight, standalone dashboard that reads Claude Code's local JSONL session
files and presents real-time usage statistics in a web UI.

Usage:
    python3 dashboard.py [--port 8199] [--claude-dir ~/.claude]

No external dependencies required - uses only Python stdlib.
"""

import argparse
import json
import os
import glob
import time
import re
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from collections import defaultdict

# ── Pricing (USD per 1M tokens, as of 2025) ──────────────────────────────────
MODEL_PRICING = {
    "claude-opus-4-6":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0, "cache_write": 1.0, "cache_read": 0.08},
    # Fallback for unknown models
    "_default": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
}


def parse_timestamp(ts) -> datetime:
    """Parse ISO 8601 timestamp string or epoch ms to datetime."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000)
    if isinstance(ts, str):
        # Handle ISO format like "2026-03-13T06:58:06.669Z"
        ts = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(ts).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def ts_to_epoch_ms(ts):
    """Convert timestamp to epoch ms for JSON serialization."""
    dt = parse_timestamp(ts)
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def get_pricing(model: str) -> dict:
    """Get pricing for a model, with fuzzy matching."""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    for key in MODEL_PRICING:
        if key in model or model in key:
            return MODEL_PRICING[key]
    return MODEL_PRICING["_default"]


def calc_cost(usage: dict, model: str) -> float:
    """Calculate cost in USD from usage dict."""
    pricing = get_pricing(model)
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)

    cost = (
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"]
        + (cache_create / 1_000_000) * pricing["cache_write"]
        + (cache_read / 1_000_000) * pricing["cache_read"]
    )
    return cost


def parse_sessions(claude_dir: str) -> dict:
    """Parse all session JSONL files and return structured data."""
    projects_dir = os.path.join(claude_dir, "projects")
    if not os.path.exists(projects_dir):
        return {"sessions": [], "summary": {}}

    sessions = []
    all_messages = []

    # Find all JSONL files (including subagents)
    jsonl_files = glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))
    jsonl_files += glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl"))

    # Group by session (top-level JSONL = session, subagent files belong to parent)
    session_files = defaultdict(list)
    for f in jsonl_files:
        parts = Path(f).parts
        # Find the session ID (UUID pattern)
        for i, part in enumerate(parts):
            if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-', part.replace('.jsonl', '')):
                session_id = part.replace('.jsonl', '')
                # Get project name
                proj_idx = list(parts).index('projects') + 1
                project = parts[proj_idx] if proj_idx < len(parts) else "unknown"
                session_files[(session_id, project)].append(f)
                break

    for (session_id, project), files in session_files.items():
        session_data = {
            "id": session_id,
            "project": project.replace("-Users-bytedance-", "").replace("-Users-bytedance", "~"),
            "messages": [],
            "total_input": 0,
            "total_output": 0,
            "total_cache_write": 0,
            "total_cache_read": 0,
            "total_cost": 0.0,
            "models": defaultdict(lambda: {"input": 0, "output": 0, "cost": 0.0, "count": 0}),
            "start_time": None,
            "end_time": None,
            "msg_count": 0,
        }

        for filepath in files:
            is_subagent = "subagents" in filepath
            try:
                with open(filepath, "r") as f:
                    for line in f:
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

                        timestamp = d.get("timestamp")
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cost = calc_cost(usage, model)

                        msg_data = {
                            "timestamp": timestamp,
                            "model": model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cache_write": cache_create,
                            "cache_read": cache_read,
                            "cost": cost,
                            "is_subagent": is_subagent,
                            "session_id": session_id,
                            "project": session_data["project"],
                        }

                        session_data["messages"].append(msg_data)
                        all_messages.append(msg_data)

                        session_data["total_input"] += input_tokens
                        session_data["total_output"] += output_tokens
                        session_data["total_cache_write"] += cache_create
                        session_data["total_cache_read"] += cache_read
                        session_data["total_cost"] += cost
                        session_data["msg_count"] += 1

                        m = session_data["models"][model]
                        m["input"] += input_tokens
                        m["output"] += output_tokens
                        m["cost"] += cost
                        m["count"] += 1

                        ts_ms = ts_to_epoch_ms(timestamp)
                        if ts_ms:
                            if session_data["start_time"] is None or ts_ms < session_data["start_time"]:
                                session_data["start_time"] = ts_ms
                            if session_data["end_time"] is None or ts_ms > session_data["end_time"]:
                                session_data["end_time"] = ts_ms
            except (OSError, IOError):
                continue

        # Convert defaultdict to regular dict for JSON serialization
        session_data["models"] = dict(session_data["models"])
        if session_data["msg_count"] > 0:
            sessions.append(session_data)

    # Sort sessions by start time (most recent first)
    sessions.sort(key=lambda s: s.get("start_time") or 0, reverse=True)

    # Build summary
    total_cost = sum(s["total_cost"] for s in sessions)
    total_input = sum(s["total_input"] for s in sessions)
    total_output = sum(s["total_output"] for s in sessions)
    total_cache_write = sum(s["total_cache_write"] for s in sessions)
    total_cache_read = sum(s["total_cache_read"] for s in sessions)
    total_msgs = sum(s["msg_count"] for s in sessions)

    # Model breakdown
    model_totals = defaultdict(lambda: {"input": 0, "output": 0, "cost": 0.0, "count": 0})
    for s in sessions:
        for model, stats in s["models"].items():
            m = model_totals[model]
            m["input"] += stats["input"]
            m["output"] += stats["output"]
            m["cost"] += stats["cost"]
            m["count"] += stats["count"]

    # Timeline data (hourly buckets for last 24h, daily for older)
    now_ms = int(time.time() * 1000)
    timeline_hourly = defaultdict(lambda: {"cost": 0.0, "input": 0, "output": 0, "count": 0})
    timeline_daily = defaultdict(lambda: {"cost": 0.0, "input": 0, "output": 0, "count": 0})

    for msg in all_messages:
        ts = msg.get("timestamp")
        if not ts:
            continue
        dt = parse_timestamp(ts)
        if dt is None:
            continue
        hour_key = dt.strftime("%Y-%m-%d %H:00")
        day_key = dt.strftime("%Y-%m-%d")

        timeline_hourly[hour_key]["cost"] += msg["cost"]
        timeline_hourly[hour_key]["input"] += msg["input_tokens"]
        timeline_hourly[hour_key]["output"] += msg["output_tokens"]
        timeline_hourly[hour_key]["count"] += 1

        timeline_daily[day_key]["cost"] += msg["cost"]
        timeline_daily[day_key]["input"] += msg["input_tokens"]
        timeline_daily[day_key]["output"] += msg["output_tokens"]
        timeline_daily[day_key]["count"] += 1

    # Today's stats
    today = datetime.now().strftime("%Y-%m-%d")
    today_data = timeline_daily.get(today, {"cost": 0.0, "input": 0, "output": 0, "count": 0})

    return {
        "sessions": [
            {
                "id": s["id"],
                "project": s["project"],
                "msg_count": s["msg_count"],
                "total_input": s["total_input"],
                "total_output": s["total_output"],
                "total_cache_write": s["total_cache_write"],
                "total_cache_read": s["total_cache_read"],
                "total_cost": round(s["total_cost"], 6),
                "models": {k: {**v, "cost": round(v["cost"], 6)} for k, v in s["models"].items()},
                "start_time": s["start_time"],
                "end_time": s["end_time"],
            }
            for s in sessions
        ],
        "summary": {
            "total_sessions": len(sessions),
            "total_messages": total_msgs,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_write_tokens": total_cache_write,
            "total_cache_read_tokens": total_cache_read,
            "total_cost": round(total_cost, 4),
            "today_cost": round(today_data["cost"], 4),
            "today_messages": today_data["count"],
            "today_input": today_data["input"],
            "today_output": today_data["output"],
            "models": {k: {**v, "cost": round(v["cost"], 6)} for k, v in model_totals.items()},
        },
        "timeline": {
            "hourly": dict(sorted(timeline_hourly.items())),
            "daily": dict(sorted(timeline_daily.items())),
        },
    }


# ── HTML Dashboard ────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #0f1117;
  --card: #1a1d27;
  --card-hover: #22263a;
  --border: #2a2e3f;
  --text: #e4e6ef;
  --text-dim: #8b8fa3;
  --accent: #7c6aef;
  --accent-light: #a78bfa;
  --green: #22c55e;
  --orange: #f59e0b;
  --red: #ef4444;
  --blue: #3b82f6;
  --cyan: #06b6d4;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', 'Inter', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}
.header {
  padding: 20px 32px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header h1 { font-size: 20px; font-weight: 600; }
.header h1 span { color: var(--accent); }
.header-right { display: flex; align-items: center; gap: 16px; }
.refresh-btn {
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}
.refresh-btn:hover { background: var(--card-hover); border-color: var(--accent); }
.last-update { font-size: 12px; color: var(--text-dim); }
.auto-refresh { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-dim); }
.auto-refresh input { accent-color: var(--accent); }

.container { padding: 24px 32px; max-width: 1400px; margin: 0 auto; }

/* ── Metric Cards ── */
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }
.metric-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  transition: all 0.2s;
}
.metric-card:hover { border-color: var(--accent); transform: translateY(-1px); }
.metric-label { font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.metric-value { font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; }
.metric-sub { font-size: 12px; color: var(--text-dim); margin-top: 6px; }
.metric-value.cost { color: var(--green); }
.metric-value.tokens { color: var(--blue); }
.metric-value.sessions { color: var(--accent); }
.metric-value.messages { color: var(--cyan); }

/* ── Charts ── */
.charts { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 24px; }
.chart-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
}
.chart-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.chart-tabs { display: flex; gap: 4px; }
.chart-tab {
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  background: transparent;
  color: var(--text-dim);
  border: none;
  transition: all 0.2s;
}
.chart-tab.active { background: var(--accent); color: white; }
.chart-tab:hover:not(.active) { background: var(--card-hover); color: var(--text); }

/* ── Session Table ── */
.table-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  margin-bottom: 24px;
}
.table-title { font-size: 14px; font-weight: 600; margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; padding: 10px 12px; border-bottom: 1px solid var(--border); }
td { padding: 12px; font-size: 13px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
tr:hover td { background: var(--card-hover); }
.session-id { font-family: 'SF Mono', monospace; font-size: 12px; color: var(--accent-light); }
.project-name { color: var(--cyan); font-size: 12px; }
.model-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  margin: 1px 2px;
}
.model-opus { background: rgba(239,68,68,0.15); color: #fca5a5; }
.model-sonnet { background: rgba(124,106,239,0.15); color: #c4b5fd; }
.model-haiku { background: rgba(6,182,212,0.15); color: #67e8f9; }
.cost-cell { color: var(--green); font-weight: 600; }
.token-cell { color: var(--blue); }

/* ── Model breakdown ── */
.model-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
.model-card {
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
}
.model-name { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
.model-stat { display: flex; justify-content: space-between; font-size: 12px; padding: 4px 0; }
.model-stat-label { color: var(--text-dim); }

/* ── Loading ── */
.loading {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 200px;
  color: var(--text-dim);
}
.spinner {
  width: 24px; height: 24px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-right: 12px;
}
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 768px) {
  .container { padding: 16px; }
  .charts { grid-template-columns: 1fr; }
  .metrics { grid-template-columns: repeat(2, 1fr); }
  .header { padding: 16px; }
}
</style>
</head>
<body>

<div class="header">
  <h1><span>&#9671;</span> Claude Code Monitor</h1>
  <div class="header-right">
    <div class="auto-refresh">
      <input type="checkbox" id="autoRefresh" checked>
      <label for="autoRefresh">Auto-refresh (30s)</label>
    </div>
    <span class="last-update" id="lastUpdate"></span>
    <button class="refresh-btn" onclick="loadData()">Refresh</button>
  </div>
</div>

<div class="container">
  <div class="metrics" id="metrics">
    <div class="loading"><div class="spinner"></div>Loading...</div>
  </div>

  <div class="charts">
    <div class="chart-card">
      <div class="chart-title">
        <span>Usage Timeline</span>
        <div class="chart-tabs">
          <button class="chart-tab active" onclick="switchTimeline('cost', this)">Cost</button>
          <button class="chart-tab" onclick="switchTimeline('tokens', this)">Tokens</button>
          <button class="chart-tab" onclick="switchTimeline('count', this)">Requests</button>
        </div>
      </div>
      <canvas id="timelineChart" height="260"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-title"><span>Model Distribution</span></div>
      <canvas id="modelChart" height="260"></canvas>
    </div>
  </div>

  <div class="table-card">
    <div class="chart-title">
      <span>Model Breakdown</span>
    </div>
    <div class="model-grid" id="modelGrid"></div>
  </div>

  <div class="table-card">
    <div class="table-title">Sessions</div>
    <table>
      <thead>
        <tr>
          <th>Session</th>
          <th>Project</th>
          <th>Models</th>
          <th>Messages</th>
          <th>Input Tokens</th>
          <th>Output Tokens</th>
          <th>Cache (W/R)</th>
          <th>Cost</th>
          <th>Time</th>
        </tr>
      </thead>
      <tbody id="sessionsBody"></tbody>
    </table>
  </div>
</div>

<script>
let dashData = null;
let timelineChart = null;
let modelChart = null;
let autoRefreshTimer = null;

function fmt(n) {
  if (n >= 1_000_000_000) return (n/1_000_000_000).toFixed(1) + 'B';
  if (n >= 1_000_000) return (n/1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n/1_000).toFixed(1) + 'K';
  return n.toString();
}

function fmtCost(n) {
  if (n >= 1) return '$' + n.toFixed(2);
  if (n >= 0.01) return '$' + n.toFixed(3);
  return '$' + n.toFixed(4);
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  const mon = (d.getMonth()+1).toString().padStart(2,'0');
  const day = d.getDate().toString().padStart(2,'0');
  const h = d.getHours().toString().padStart(2,'0');
  const m = d.getMinutes().toString().padStart(2,'0');
  return `${mon}-${day} ${h}:${m}`;
}

function modelBadge(model) {
  let cls = 'model-sonnet';
  let name = model;
  if (model.includes('opus')) { cls = 'model-opus'; name = 'Opus'; }
  else if (model.includes('sonnet')) { cls = 'model-sonnet'; name = 'Sonnet'; }
  else if (model.includes('haiku')) { cls = 'model-haiku'; name = 'Haiku'; }
  return `<span class="model-badge ${cls}">${name}</span>`;
}

function renderMetrics(data) {
  const s = data.summary;
  document.getElementById('metrics').innerHTML = `
    <div class="metric-card">
      <div class="metric-label">Total Cost</div>
      <div class="metric-value cost">${fmtCost(s.total_cost)}</div>
      <div class="metric-sub">Today: ${fmtCost(s.today_cost)}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Total Tokens</div>
      <div class="metric-value tokens">${fmt(s.total_input_tokens + s.total_output_tokens)}</div>
      <div class="metric-sub">In: ${fmt(s.total_input_tokens)} / Out: ${fmt(s.total_output_tokens)}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Sessions</div>
      <div class="metric-value sessions">${s.total_sessions}</div>
      <div class="metric-sub">${s.total_messages} messages total</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Today</div>
      <div class="metric-value messages">${s.today_messages}</div>
      <div class="metric-sub">${fmt(s.today_input + s.today_output)} tokens / ${fmtCost(s.today_cost)}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Cache Tokens</div>
      <div class="metric-value" style="color:var(--orange)">${fmt(s.total_cache_write_tokens + s.total_cache_read_tokens)}</div>
      <div class="metric-sub">Write: ${fmt(s.total_cache_write_tokens)} / Read: ${fmt(s.total_cache_read_tokens)}</div>
    </div>
  `;
}

function renderTimeline(data, metric='cost') {
  const daily = data.timeline.daily;
  const labels = Object.keys(daily);
  let values, label, color;
  if (metric === 'cost') {
    values = labels.map(k => daily[k].cost);
    label = 'Cost ($)';
    color = '#22c55e';
  } else if (metric === 'tokens') {
    values = labels.map(k => daily[k].input + daily[k].output);
    label = 'Tokens';
    color = '#3b82f6';
  } else {
    values = labels.map(k => daily[k].count);
    label = 'Requests';
    color = '#7c6aef';
  }

  if (timelineChart) timelineChart.destroy();
  const ctx = document.getElementById('timelineChart').getContext('2d');
  timelineChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: labels.map(l => l.slice(5)),
      datasets: [{
        label: label,
        data: values,
        backgroundColor: color + '33',
        borderColor: color,
        borderWidth: 1.5,
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              if (metric === 'cost') return fmtCost(ctx.raw);
              return fmt(ctx.raw);
            }
          }
        }
      },
      scales: {
        x: { grid: { color: '#2a2e3f' }, ticks: { color: '#8b8fa3', font: { size: 11 } } },
        y: {
          grid: { color: '#2a2e3f' },
          ticks: {
            color: '#8b8fa3',
            font: { size: 11 },
            callback: function(v) { return metric === 'cost' ? fmtCost(v) : fmt(v); }
          }
        }
      }
    }
  });
}

function renderModelChart(data) {
  const models = data.summary.models;
  const names = Object.keys(models);
  const costs = names.map(n => models[n].cost);
  const colors = names.map(n => {
    if (n.includes('opus')) return '#ef4444';
    if (n.includes('haiku')) return '#06b6d4';
    return '#7c6aef';
  });

  if (modelChart) modelChart.destroy();
  const ctx = document.getElementById('modelChart').getContext('2d');
  modelChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: names.map(n => {
        if (n.includes('opus')) return 'Opus';
        if (n.includes('haiku')) return 'Haiku';
        if (n.includes('sonnet')) return 'Sonnet';
        return n;
      }),
      datasets: [{
        data: costs,
        backgroundColor: colors,
        borderWidth: 0,
        hoverOffset: 4,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '65%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: '#e4e6ef', font: { size: 12 }, padding: 16 }
        },
        tooltip: {
          callbacks: { label: ctx => `${ctx.label}: ${fmtCost(ctx.raw)}` }
        }
      }
    }
  });
}

function renderModelGrid(data) {
  const models = data.summary.models;
  let html = '';
  for (const [name, stats] of Object.entries(models)) {
    let displayName = name;
    if (name.includes('opus')) displayName = 'Claude Opus 4.6';
    else if (name.includes('sonnet')) displayName = 'Claude Sonnet 4.6';
    else if (name.includes('haiku')) displayName = 'Claude Haiku 4.5';

    html += `
      <div class="model-card">
        <div class="model-name">${modelBadge(name)} ${displayName}</div>
        <div class="model-stat"><span class="model-stat-label">Requests</span><span>${stats.count}</span></div>
        <div class="model-stat"><span class="model-stat-label">Input Tokens</span><span>${fmt(stats.input)}</span></div>
        <div class="model-stat"><span class="model-stat-label">Output Tokens</span><span>${fmt(stats.output)}</span></div>
        <div class="model-stat"><span class="model-stat-label">Cost</span><span class="cost-cell">${fmtCost(stats.cost)}</span></div>
      </div>`;
  }
  document.getElementById('modelGrid').innerHTML = html;
}

function renderSessions(data) {
  let html = '';
  for (const s of data.sessions) {
    const models = Object.keys(s.models).map(m => modelBadge(m)).join('');
    html += `<tr>
      <td class="session-id">${s.id.slice(0,8)}...</td>
      <td class="project-name">${s.project || '~'}</td>
      <td>${models}</td>
      <td>${s.msg_count}</td>
      <td class="token-cell">${fmt(s.total_input)}</td>
      <td class="token-cell">${fmt(s.total_output)}</td>
      <td style="color:var(--orange);font-size:12px">${fmt(s.total_cache_write)} / ${fmt(s.total_cache_read)}</td>
      <td class="cost-cell">${fmtCost(s.total_cost)}</td>
      <td style="font-size:12px;color:var(--text-dim)">${fmtTime(s.start_time)}</td>
    </tr>`;
  }
  document.getElementById('sessionsBody').innerHTML = html;
}

function switchTimeline(metric, btn) {
  document.querySelectorAll('.chart-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  if (dashData) renderTimeline(dashData, metric);
}

async function loadData() {
  try {
    const resp = await fetch('/api/data');
    dashData = await resp.json();
    renderMetrics(dashData);
    renderTimeline(dashData, 'cost');
    renderModelChart(dashData);
    renderModelGrid(dashData);
    renderSessions(dashData);
    document.getElementById('lastUpdate').textContent = 'Updated: ' + new Date().toLocaleTimeString();
  } catch(e) {
    console.error('Failed to load data:', e);
  }
}

// Auto-refresh
document.getElementById('autoRefresh').addEventListener('change', function() {
  if (this.checked) {
    autoRefreshTimer = setInterval(loadData, 30000);
  } else {
    clearInterval(autoRefreshTimer);
  }
});

loadData();
autoRefreshTimer = setInterval(loadData, 30000);
</script>
</body>
</html>"""


# ── HTTP Server ───────────────────────────────────────────────────────────────

class DashboardHandler(SimpleHTTPRequestHandler):
    claude_dir = os.path.expanduser("~/.claude")

    def log_message(self, format, *args):
        # Quieter logs
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif parsed.path == "/api/data":
            data = parse_sessions(self.claude_dir)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        else:
            self.send_error(404)


def main():
    parser = argparse.ArgumentParser(description="Claude Code Usage Monitor Dashboard")
    parser.add_argument("--port", type=int, default=8199, help="Port to serve on (default: 8199)")
    parser.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"), help="Claude Code data directory")
    args = parser.parse_args()

    DashboardHandler.claude_dir = args.claude_dir

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print(f"\n  ◇ Claude Code Monitor Dashboard")
    print(f"  ├─ URL:        http://localhost:{args.port}")
    print(f"  ├─ Data dir:   {args.claude_dir}")
    print(f"  └─ Auto-refresh: 30s\n")
    print(f"  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
