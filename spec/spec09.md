# spec09.md — F8 Metrics v2: Time Series, Per-Agent & Export

**Phase P20 · Wave 3 · Owns no new invariant · Depends on P16**

`GET /metrics/overview` answers "how are we doing right now" and nothing else. It has no time axis,
so no one can tell whether a 12% breach rate is an improvement or a collapse; no per-agent view, so
workload imbalance is invisible; and no export, so any question the endpoint does not answer cannot
be answered at all.

---

## 1. Goal

Three additions: a time axis, a per-agent cut, and a way to get the underlying rows out.

---

## 2. Ground Rules

**Read-only, no unit of work.** `MetricsService` already takes a repository rather than a UoW,
because it opens no transaction and writes nothing. Every addition here keeps that shape.

**SQL in the repository, arithmetic in the service.** The existing split holds — and the existing
comment in `metrics_service.py` about summing before dividing rather than averaging per-group
averages is the trap to avoid repeating in every new aggregate.

**Aggregates must account for the pause.** After spec05, three definitions change and every new
metric must use the corrected form:

| Measure | Correct definition |
|---|---|
| SLA met | `resolved_at <= sla_due_at` (not `deadline`) |
| Breach | `sla_breached_at IS NOT NULL`; open breaches exclude `PENDING_CUSTOMER` |
| Resolution time | `resolved_at - created_at` **minus** `sla_paused_seconds` |

Reporting raw wall-clock resolution time after building a pause feature would mean the pause
improves the SLA number while making the reported handling time look worse — a metric that
contradicts the feature it measures. Report both: `avg_resolution_seconds` (wall clock, for customer
experience) and `avg_working_seconds` (pause-excluded, for agent performance). They answer different
questions and neither is a substitute for the other.

---

## 3. Time Series

### GET /api/v1/metrics/timeseries — **new**
Auth: required · Authz: `ADMIN`
Query: `?from=&to=&bucket=day|week|month&metric=created|resolved|breached|breach_rate`

```json
{
  "bucket": "day", "from": "...", "to": "...",
  "points": [{ "bucket_start": "2026-08-01T00:00:00Z", "value": 42 }]
}
```

- Bucketing via `date_trunc` in the repository, over `created_at` for `created`, `resolved_at` for
  `resolved`, `sla_breached_at` for `breached`.
- **Empty buckets are emitted as zero**, generated from `generate_series` and left-joined to the
  aggregate. A chart that silently omits a quiet day draws a straight line through the gap and
  misreports it as steady activity.
- Range capped at 366 buckets; over that → **422**, rather than letting a client ask for a decade of
  daily points.
- All bucketing is UTC, matching the `timestamptz` storage. Timezone-aware bucketing is out of scope
  and stated here so it is a known limitation rather than an assumed feature.

---

## 4. Per-Agent Metrics

### GET /api/v1/metrics/agents — **new**
Auth: required · Authz: `ADMIN`

```json
{ "items": [{
  "agent": { "id": "uuid", "name": "...", "is_active": true },
  "open_tickets": 12, "in_progress": 5, "pending_customer": 2,
  "resolved_in_period": 40,
  "sla_met_rate": 0.925,
  "avg_working_seconds": 4100,
  "current_load_pct": 0.60
}] }
```

Query: `?from=&to=`. Grouped on `ticket.assignee_id`.

**Attribution is by current assignee, and the response says so.** A reassigned ticket counts entirely
toward whoever holds it now. True per-agent attribution would require reconstructing custody windows
from `ASSIGNMENT` events and apportioning time between them — real work, and out of scope for this
phase. Documenting the simplification is mandatory: an unlabelled approximation in a performance
metric is worse than no metric, because someone will manage against it.

**Inactive agents are included** when they have tickets in range. Excluding them would make the
period's totals not add up, and a departed agent's history is exactly what a period report is for.

`DISPATCHER` is denied, as with all metrics (`docs/AUTHORIZATION.md §3`). Per-agent performance data
is management information.

---

## 5. Breakdowns

### GET /api/v1/metrics/overview — extended
Gains `by_tier` and `by_category` alongside the existing `by_priority`, in the same
`PriorityMetrics`-shaped form, plus the two new top-level fields:

```json
{ "...": "existing fields",
  "avg_working_seconds": 3900,
  "pending_customer": 3,
  "by_tier": { "ENTERPRISE": {...} },
  "by_category": { "OUTAGE": {...} } }
```

`pending_customer` joins the status counts — after spec05 the four-status breakdown no longer sums to
the total, which would make the dashboard visibly wrong.

Every dictionary is filled for all enum members even at zero, following the existing
`by_priority.setdefault` rule: a stable response shape means the dashboard never has to branch on
missing keys.

---

## 6. Export

### GET /api/v1/metrics/export/tickets.csv — **new**
Auth: required · Authz: `ADMIN`
Query: the **same filter set as `GET /tickets`** (spec04), so an admin exports exactly the view they
are looking at rather than learning a second filter language.

- `text/csv` with `Content-Disposition: attachment`.
- **Streamed** via `StreamingResponse` over a server-side cursor, never assembled in memory. This
  process hosts the SLA monitor; materializing 100k rows would stall breach detection, the same
  reason attachment uploads stream (spec03 §3).
- Row cap `APP_EXPORT_MAX_ROWS` (default 50,000); exceeded → **422** telling the caller to narrow the
  range. A truncated export that does not say it was truncated is a wrong answer.
- Columns: ticket id, customer name, tier, category, priority, status, assignee name, created,
  deadline, `sla_due_at`, paused seconds, resolved, breached, escalation level, reopen count.
- **No ticket body, no comment text.** The export is operational data, not a content dump; bodies and
  comments carry whatever a customer pasted into them.

**CSV injection.** Any field beginning `=`, `+`, `-`, or `@` is prefixed with a single quote before
writing. A spreadsheet treats a leading `=` in an untrusted subject line as a formula, and every one
of these fields is customer-supplied text.

---

## 7. Performance

Metrics scan the whole ticket table by construction. Two supporting indexes:

```sql
CREATE INDEX ix_ticket_resolved_at ON ticket (resolved_at) WHERE resolved_at IS NOT NULL;
CREATE INDEX ix_ticket_breached_at ON ticket (sla_breached_at) WHERE sla_breached_at IS NOT NULL;
```

**Migration `0017_metrics_indexes`.**

**No materialized views, no caching.** Both are the documented answer if these queries ever get slow
at real volume — and `docs/ARCHITECTURE.md §6` sets the precedent for that posture: measure first,
then act, and record the trigger. Adding a refresh schedule now would introduce staleness and a
second background task for a problem that does not exist yet.

---

## 8. Tests

`tests/unit`
- Rate and average helpers: zero denominators, the sum-then-divide rule across groups.
- Working-time arithmetic subtracts `sla_paused_seconds` and never returns negative.
- CSV field escaping for all four injection prefixes.

`tests/integration` (`@pytest.mark.db`)
- Time series emits zero-valued buckets for empty days; boundary tickets land in exactly one bucket.
- Range over 366 buckets → 422.
- A paused-then-resolved ticket: `avg_resolution_seconds` uses wall clock,
  `avg_working_seconds` excludes the pause, and SLA-met is judged on `sla_due_at`.
- `by_tier` / `by_category` include zero entries for every enum member.
- Status counts including `pending_customer` sum to the ticket total.
- Export streams above the in-memory threshold; over the row cap → 422.
- Per-agent totals include inactive agents holding tickets.

`tests/api`
- All new endpoints: `ADMIN` 200; `AGENT`, `DISPATCHER`, `CUSTOMER` → 403.
- CSV content type and `Content-Disposition`.
- Export filters match `GET /tickets` results for the same query.

---

## 9. Definition of Done

`0016` applies; every aggregate uses `sla_due_at` and pause-corrected working time; empty buckets are
present as zeros; the export streams, caps, and escapes; the per-agent attribution simplification is
documented in the API response description, not only in this file.
