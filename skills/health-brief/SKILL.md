---
name: health-brief
description: "Use when reporting on Apple Health data — sleep, activity, heart, workouts."
version: 1.0.0
author: Tyler Amberry
license: MIT
metadata:
  hermes:
    tags: [health, sleep, activity, heart-rate, workouts, brief]
    related_skills: [health-ingest]
---

# Reporting Apple Health

The store holds data the phone pushed. Answer from the tools, never from memory or
assumption, and never compare a number to a baseline the store cannot show you.

## Procedure

**1. Check the store is current before quoting anything.**

```
health_status
```

If `freshness.hours_since_data` is large, say so in the answer — "last night's
sleep is from three days ago" is a different statement from silence. If the store
is empty, the phone has not pushed yet: say that and stop; do not construct a
reading from the export's old data without labelling the date.

**2. Start wide, then narrow.**

- `health_summary` — rings, steps, resting HR, HRV, last night's sleep, workouts,
  and which numbers moved versus the previous window. This is the brief.
- `health_sleep` — nights with stages when sleep is the subject.
- `health_query` — one metric as a daily series when a trend is the subject.
- `health_workouts` — sessions and rest days.
- `health_metrics` — what is actually stored, before guessing a metric name.

**3. Quote numbers with their context.** Every block carries the window, the
sample count, and the unit. "7.1h asleep, 7-night average 6.4h, bedtime drifted
49 minutes later" beats "you slept more".

**4. Flag data gaps instead of papering over them.** In particular: workouts are
**not** readable by iOS Shortcuts. If `health_workouts` is empty and the phone
only pushes Shortcuts data, say that workouts need the Health export or Health
Auto Export — that is a known limit, not a bad week.

**5. Dashboard on request.** `health_dashboard` writes the widget and returns its
path; then emit `::preview{file="<path>"}` alone on a line so it renders in chat.

## Reading the numbers correctly

- **`asleep` excludes `in_bed` and `awake`.** A 8.5h "in bed" night with 7h
  asleep is a 7h night. Efficiency is asleep ÷ elapsed.
- **Bedtimes past midnight.** 00:35 sorts after 23:40 in bedtime comparisons; the
  store already unwraps them, so use the reported averages rather than diffing
  clock strings.
- **Naps are flagged, not averaged in.** Check `is_nap` before treating a session
  as "last night".
- **Aggregations differ by metric**, because summing a heart rate is meaningless:
  steps/energy/distance sum, resting HR/HRV/SpO2 average, weight/VO₂max take the
  last value. `health_metrics` reports which rule applies.
- **Two devices, one day.** Watch and iPhone both log steps; the store merges
  sources per day rather than double counting, and `sources` lists what contributed.
- **Resting HR down and HRV up** are both the good direction. Say which direction
  is good only when the metric has a known one.

## Style

Lead with the answer, then the supporting numbers. A brief for a morning message:
last night's sleep in one line, today's rings vs goals, one thing that changed
versus the previous week, and one thing worth attention. No lecture, no metric
dump. If nothing changed, say that — a flat week is information.
