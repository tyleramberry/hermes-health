"""Dashboard widget: one self-contained HTML file, data already baked in.

Renders from the store at call time and inlines the numbers, so the widget needs
no server, no fetch, and no script permission — it opens anywhere, including the
Hermes desktop ``::preview`` pane, which is what it is built for.

Styling uses the host app's CSS variables (``--foreground``, ``--accent``,
``--border``, ``--card``, ``--muted-foreground``) with fallbacks, so it picks up
whatever theme is active instead of forcing its own.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config
from . import queries

WIDTH = 640


def _ring_svg(pct: Optional[float], label: str, value: str, color: str) -> str:
    clamped = 0.0 if pct is None else max(0.0, min(pct, 100.0))
    radius = 40
    circumference = 2 * 3.14159265 * radius
    dash = circumference * clamped / 100.0
    return f"""
    <div class="ring">
      <svg viewBox="0 0 100 100" width="104" height="104" role="img" aria-label="{label} {value}">
        <circle cx="50" cy="50" r="{radius}" fill="none" stroke="var(--border, #d8d8d8)"
                stroke-width="9" opacity="0.55"/>
        <circle cx="50" cy="50" r="{radius}" fill="none" stroke="{color}" stroke-width="9"
                stroke-linecap="round" transform="rotate(-90 50 50)"
                stroke-dasharray="{dash:.1f} {circumference:.1f}"/>
        <text x="50" y="47" text-anchor="middle" font-size="17" fill="var(--foreground, #111)"
              font-weight="600">{value}</text>
        <text x="50" y="63" text-anchor="middle" font-size="10"
              fill="var(--muted-foreground, #777)">{label}</text>
      </svg>
    </div>"""


def _sparkline(points: List[Dict[str, Any]], label: str, unit: str, digits: int = 0) -> str:
    values = [p["value"] for p in points if p.get("value") is not None]
    if len(values) < 2:
        return f'<div class="empty">Not enough {label.lower()} data yet.</div>'
    width, height, pad = 300, 70, 8
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = (width - 2 * pad) / (len(values) - 1)
    coords = [
        (pad + i * step, height - pad - (v - low) / span * (height - 2 * pad)) for i, v in enumerate(values)
    ]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    first, last = values[0], values[-1]
    delta = last - first
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "■")
    return f"""
    <div class="chart">
      <div class="chart-head">
        <span class="chart-title">{label}</span>
        <span class="chart-meta">{last:.{digits}f} {unit} <span class="delta">{arrow} {abs(delta):.{digits}f}</span></span>
      </div>
      <svg viewBox="0 0 {width} {height}" width="100%" height="{height}" preserveAspectRatio="none">
        <polyline points="{path}" fill="none" stroke="var(--accent, #0a84ff)" stroke-width="2.5"
                  stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="{coords[-1][0]:.1f}" cy="{coords[-1][1]:.1f}" r="3.5" fill="var(--accent, #0a84ff)"/>
      </svg>
      <div class="chart-foot"><span>low {low:.{digits}f}</span><span>{len(values)} days</span>
        <span>high {high:.{digits}f}</span></div>
    </div>"""


def _bars(points: List[Dict[str, Any]], label: str, goal: Optional[float] = None) -> str:
    values = [p for p in points if p.get("value") is not None]
    if not values:
        return f'<div class="empty">No {label.lower()} data yet.</div>'
    peak = max([v["value"] for v in values] + ([goal] if goal else [])) or 1.0
    bars = []
    for point in values:
        height = max(2.0, point["value"] / peak * 100.0)
        hit = goal is not None and point["value"] >= goal
        bars.append(
            f'<div class="bar-col" title="{point["day"]}: {point["value"]:,.0f}">'
            f'<div class="bar{" hit" if hit else ""}" style="height:{height:.1f}%"></div>'
            f'<span class="bar-label">{point["day"][-2:]}</span></div>'
        )
    goal_line = ""
    if goal:
        goal_line = f'<div class="goal-line" style="bottom:{goal / peak * 100:.1f}%"></div>'
    average = sum(v["value"] for v in values) / len(values)
    return f"""
    <div class="chart">
      <div class="chart-head">
        <span class="chart-title">{label}</span>
        <span class="chart-meta">avg {average:,.0f}{f" / goal {goal:,.0f}" if goal else ""}</span>
      </div>
      <div class="bars">{goal_line}{''.join(bars)}</div>
    </div>"""


def _sleep_block(sleep: Dict[str, Any]) -> str:
    last = sleep.get("last_night") or {}
    averages = sleep.get("averages") or {}
    if not last:
        return '<div class="empty">No sleep recorded yet.</div>'
    stages = [
        ("Deep", last.get("deep_hours") or 0.0, "#4c6ef5"),
        ("REM", last.get("rem_hours") or 0.0, "#9775fa"),
        ("Core", last.get("core_hours") or 0.0, "#74c0fc"),
        ("Awake", last.get("awake_hours") or 0.0, "#f783ac"),
    ]
    total = sum(v for _n, v, _c in stages) or 1.0
    segments = "".join(
        f'<div class="seg" style="width:{value / total * 100:.2f}%;background:{color}"'
        f' title="{name} {value:.2f}h"></div>'
        for name, value, color in stages
        if value > 0
    )
    legend = "".join(
        f'<span class="legend-item"><i style="background:{color}"></i>{name} {value:.2f}h</span>'
        for name, value, color in stages
        if value > 0
    )
    return f"""
    <div class="sleep">
      <div class="sleep-top">
        <span class="sleep-hours">{last.get('asleep', '--')}</span>
        <span class="sleep-meta">asleep · {last.get('day')} · {last.get('bedtime')}–{last.get('wake')}
          · {last.get('efficiency_pct')}% efficiency</span>
      </div>
      <div class="stage-bar">{segments}</div>
      <div class="legend">{legend}</div>
      <div class="chart-foot">
        <span>7-night avg {averages.get('asleep', '--')}</span>
        <span>deep {averages.get('deep_hours', '--')}h</span>
        <span>REM {averages.get('rem_hours', '--')}h</span>
        <span>bedtime {averages.get('bedtime', '--')}</span>
      </div>
    </div>"""


def _workouts_block(report: Dict[str, Any]) -> str:
    sessions = report.get("sessions") or []
    if not sessions:
        note = report.get("source_note") or "No workouts yet."
        return f'<div class="empty">{note}</div>'
    rows = []
    for session in sessions[:6]:
        bits = [f"{session['duration_minutes']:.0f} min"]
        if session.get("distance_mi"):
            bits.append(f"{session['distance_mi']:.2f} mi")
        if session.get("energy_kcal"):
            bits.append(f"{session['energy_kcal']:,.0f} Cal")
        if session.get("avg_hr"):
            bits.append(f"{session['avg_hr']:.0f} bpm avg")
        rows.append(
            f'<div class="wk"><span class="wk-day">{session["day"][5:]}</span>'
            f'<span class="wk-activity">{session["activity"].replace("_", " ")}</span>'
            f'<span class="wk-meta">{" · ".join(bits)}</span></div>'
        )
    return "".join(rows)


def render_dashboard(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    metric_days: int = 14,
    end_day: Optional[str] = None,
    title: str = "Apple Health",
) -> str:
    """Build the widget HTML from the store."""
    summary = queries.summary(conn, days=days, end_day=end_day)
    sleep = queries.sleep_report(conn, days=max(7, metric_days), end_day=end_day)
    workouts = queries.workout_report(conn, days=max(14, days * 2), end_day=end_day)
    today = summary["today"]
    rings = today["rings"]
    resting = queries.daily_series(conn, "resting_heart_rate", days=metric_days, end_day=end_day)
    steps = queries.daily_series(conn, "steps", days=metric_days, end_day=end_day)
    hrv = queries.daily_series(conn, "hrv", days=metric_days, end_day=end_day)
    freshness = summary["data"]

    ring_html = (
        _ring_svg(rings["active_energy"]["pct"], "Move", f"{rings['active_energy']['value']:,.0f}", "#ff6b6b")
        + _ring_svg(
            rings["exercise_minutes"]["pct"],
            "Exercise",
            f"{rings['exercise_minutes']['value']:,.0f}",
            "#51cf66",
        )
        + _ring_svg(rings["stand_hours"]["pct"], "Stand", f"{rings['stand_hours']['value']:,.0f}", "#22b8cf")
    )

    sync_line = "no data yet"
    if freshness.get("newest_data_iso"):
        hours = freshness.get("hours_since_data")
        sync_line = f"data through {freshness['newest_data_iso'][:16].replace('T', ' ')}"
        if hours is not None:
            sync_line += f" ({hours}h ago)" if hours >= 1 else " (fresh)"

    payload = {
        "generated_at": summary["generated_at"],
        "days": days,
        "rings": rings,
        "steps": today.get("steps_today"),
    }

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title} — hermes-health</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; width: {WIDTH}px; color: var(--foreground, #111);
    font-family: inherit; font-size: 13px; line-height: 1.4;
  }}
  h1 {{ font-size: 15px; margin: 0 0 2px; font-weight: 650; }}
  .sub {{ color: var(--muted-foreground, #777); font-size: 11.5px; margin-bottom: 12px; }}
  .rings {{ display: flex; gap: 14px; align-items: center; }}
  .stub {{ font-size: 11.5px; color: var(--muted-foreground, #777); margin-left: auto; text-align: right; }}
  .stub b {{ display: block; font-size: 16px; color: var(--foreground, #111); }}
  .card {{
    border: 1px solid var(--border, #e2e2e2); border-radius: 10px; padding: 11px 13px;
    margin-top: 12px; background: var(--card, transparent);
  }}
  .chart-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }}
  .chart-title {{ font-weight: 600; font-size: 12.5px; }}
  .chart-meta {{ color: var(--muted-foreground, #777); font-size: 11.5px; }}
  .delta {{ margin-left: 4px; }}
  .chart-foot {{
    display: flex; justify-content: space-between; color: var(--muted-foreground, #777);
    font-size: 10.5px; margin-top: 3px;
  }}
  .charts {{ display: flex; gap: 18px; }}
  .charts > * {{ flex: 1 1 0; min-width: 0; }}
  .empty {{ color: var(--muted-foreground, #777); font-size: 12px; }}
  .bars {{ position: relative; display: flex; align-items: flex-end; gap: 3px; height: 70px; }}
  .bar-col {{ flex: 1 1 0; display: flex; flex-direction: column; justify-content: flex-end;
              align-items: center; height: 100%; }}
  .bar {{ width: 100%; border-radius: 3px 3px 0 0; background: var(--accent, #0a84ff); opacity: .55; }}
  .bar.hit {{ opacity: 1; }}
  .bar-label {{ font-size: 9px; color: var(--muted-foreground, #777); margin-top: 2px; }}
  .goal-line {{ position: absolute; left: 0; right: 0; border-top: 1px dashed var(--border, #ccc); }}
  .sleep-top {{ display: flex; align-items: baseline; gap: 8px; }}
  .sleep-hours {{ font-size: 22px; font-weight: 650; }}
  .sleep-meta {{ color: var(--muted-foreground, #777); font-size: 11.5px; }}
  .stage-bar {{ display: flex; height: 14px; border-radius: 7px; overflow: hidden; margin: 8px 0 6px;
                border: 1px solid var(--border, #e2e2e2); }}
  .seg {{ height: 100%; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px;
             color: var(--muted-foreground, #777); }}
  .legend-item {{ display: inline-flex; align-items: center; gap: 4px; }}
  .legend-item i {{ width: 8px; height: 8px; border-radius: 2px; display: inline-block; }}
  .wk {{ display: flex; gap: 8px; padding: 3px 0; font-size: 12px;
         border-bottom: 1px solid var(--border, #eee); }}
  .wk:last-child {{ border-bottom: 0; }}
  .wk-day {{ width: 42px; color: var(--muted-foreground, #777); }}
  .wk-activity {{ width: 110px; font-weight: 600; text-transform: capitalize; }}
  .wk-meta {{ color: var(--muted-foreground, #777); }}
  footer {{ margin-top: 11px; color: var(--muted-foreground, #777); font-size: 10.5px;
            display: flex; justify-content: space-between; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="sub">{sync_line} · timezone {freshness.get('timezone')}</div>

  <div class="rings">
    {ring_html}
    <div class="stub"><b>{(today.get('steps_today') or 0):,.0f}</b>steps today</div>
  </div>

  <div class="card">
    <div class="charts">
      {_sparkline(resting, "Resting heart rate", "bpm")}
      {_sparkline(hrv, "HRV (SDNN)", "ms")}
    </div>
  </div>

  <div class="card">{_bars(steps, f"Steps, last {metric_days} days")}</div>

  <div class="card">{_sleep_block(sleep)}</div>

  <div class="card">
    <div class="chart-head"><span class="chart-title">Recent workouts</span>
      <span class="chart-meta">{workouts.get('n', 0)} in {workouts.get('days', 0)} days ·
        {workouts.get('total_minutes') or 0:,.0f} min total</span></div>
    {_workouts_block(workouts)}
  </div>

  <footer>
    <span>hermes-health · {len(steps)} days of steps · {freshness.get('hours_since_data')}h since last sample</span>
    <span>{payload['generated_at'][:16].replace('T', ' ')}</span>
  </footer>
</body>
</html>
"""
