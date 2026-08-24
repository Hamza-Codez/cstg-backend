# spec08.md — F7 Notifications & Unread Tracking

**Phase P19 · Wave 3 · Owns INV-17 · Depends on P13, P16**

`docs/UIUX_FRONTEND.md §5` puts a `Bell` in the top bar and `§7.1.6` says *"A `Bell` badge flags new
replies."* Neither exists. Without it, every participant has to poll the UI by hand: an agent
refreshes to find out a customer answered, and a customer refreshes to find out support replied.

---

## 1. Goal

Every principal can see what changed on the tickets they can already see, since they last looked.

---

## 2. Design: A Read Model, Not a Table of Rows

The obvious implementation writes a `notification` row per recipient per event. This spec does not do
that.

| Approach | Cost |
|---|---|
| Row per recipient per event | Fan-out writes on the hot path. Every state change gains N inserts inside its transaction — the transaction that must stay fast because the SLA monitor shares the process. Recipient sets must be computed at write time and go stale when assignment changes. And the rows duplicate `ticket_event`, which already records everything. |
| **Read model over `ticket_event` + a per-principal cursor** | **Chosen.** Zero writes on the hot path. `ticket_event` is already append-only, already indexed by `(ticket_id, created_at)`, and already the record of every state change. Recipient scoping is computed at *read* time from current visibility, so it is never stale. |

The only new write is one cursor row per principal, updated when they mark things read.

> **Unread = the events on tickets I can currently read, created after my cursor, of a type I am
> allowed to see, that I did not cause.**

**Why not websockets or a broker.** `docs/ARCHITECTURE.md §10` freezes both, and neither is needed:
this is a badge count and a short list, and the client fetches it on navigation plus a slow interval
(spec08 frontend, §4). Push would add infrastructure to make a number update a few seconds sooner.

---

## 3. Data Model

```sql
CREATE TABLE notification_cursor (
  principal_type actor_type NOT NULL CHECK (principal_type <> 'SYSTEM'),
  principal_id   uuid       NOT NULL,
  last_read_at   timestamptz NOT NULL,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_type, principal_id)
);
```

Reusing the `actor_type` enum keeps one vocabulary for "who" across `ticket_event`, JWT claims, and
this table — the alignment `docs/AUTHORIZATION.md §1` already insists on. `SYSTEM` is excluded by
CHECK: the monitor does not read its own notifications.

No foreign key, because the column points into two tables depending on `principal_type` — the same
polymorphism `ticket_event.actor_id` already lives with. Unlike spec02's comment author, an
exclusive-arc pair earns nothing here: this table is derived state that can be rebuilt from nothing,
not an audit record.

**A missing cursor row means "never looked"**, and the read model treats it as `created_at` of the
principal's account. New users do not get a badge counting the entire history of the system.

**Migration `0016_notification_cursor`.** Table only; no backfill. Every principal starts with no
cursor and therefore a first-visit view of recent activity on their own tickets, which is correct.

---

## 4. INV-17 — The Whole Security Model

> **INV-17.** A notification never reveals an event on a ticket its recipient may not read.

Notifications are a second read path to `ticket_event`, and a second read path is a second chance to
leak. The implementation must not build its own idea of visibility.

**Rule: the notification query reuses the exact scope predicate from `TicketService.list_tickets`.**
Customer → `ticket.customer_id = :me`; agent → `ticket.assignee_id = :me`; dispatcher/admin →
unrestricted. One helper builds that predicate and both call sites use it. If a role's scope ever
changes, notifications change with it and cannot be forgotten.

**Event type filtering layers on top**, reusing `TicketService._CUSTOMER_VISIBLE_EVENTS`. Customers
are notified only about `CREATED`, `STATUS_CHANGE`, and `SLA_BREACH` — and about `COMMENT` events
**only for `PUBLIC_REPLY`**, determined from the event's `detail.type`, which `CommentService`
already writes. Internal notes must not produce a customer notification, not even a contentless one:
a badge that appears when an internal note is written leaks the note's existence and timing, which is
precisely what INV-9 forbids.

**Self-authored events are excluded** (`actor_id <> :me`). "You resolved a ticket" is not news.

**Scope is dynamic, and that is the point.** An agent unassigned from a ticket immediately stops
seeing its notifications, including ones generated while they held it. A row-per-recipient design
would have to hunt down and delete those rows; here it falls out of the query.

---

## 5. Query

```sql
SELECT e.*, t.subject, t.status
  FROM ticket_event e
  JOIN ticket t ON t.id = e.ticket_id
 WHERE <role scope predicate>
   AND e.created_at > :cursor
   AND e.actor_id IS DISTINCT FROM :principal_id
   AND e.type = ANY(:visible_types)
 ORDER BY e.created_at DESC
 LIMIT :limit;
```

`IS DISTINCT FROM` rather than `<>` — `actor_id` is NULL for `SYSTEM` events, and `<>` would silently
drop every SLA breach notification. This is the kind of NULL bug that ships quietly and is only
noticed when someone asks why breaches never notify.

Served by the existing `ix_event_ticket` plus the ticket-scope indexes. A supporting index on
`ticket_event (created_at DESC)` is added for the unscoped dispatcher/admin case.

**Count is capped.** The badge reports `min(count, 99)` computed with `LIMIT 100`, never an unbounded
`COUNT(*)` over the audit log. A badge showing "99+" is exactly as useful as one showing 4,213 and
costs a bounded query.

---

## 6. API Contract — `docs/API.md` new §14

### GET /api/v1/notifications
Auth: required · Authz: any role, **scope differs** (§4).
Query: `?limit=` (default 20, max 50).

```json
{
  "items": [{
    "event_id": "uuid", "ticket_id": "uuid", "ticket_subject": "...",
    "type": "STATUS_CHANGE", "actor_type": "USER", "actor_name": "Dana Reed",
    "from_status": "OPEN", "to_status": "IN_PROGRESS", "created_at": "..."
  }],
  "unread_count": 7,
  "last_read_at": "..."
}
```

`ticket_subject` and `actor_name` are joined in so the client renders a line without N+1 lookups.
Both are already visible to the recipient by construction — if the ticket were not readable, the
event would not be in the result.

### POST /api/v1/notifications/read
Auth: required. Body: `{ "up_to": "timestamp?" }` — defaults to now. Upserts the cursor. Response
200 with the new `unread_count`.

`up_to` is clamped so a cursor can only move **forward**. Allowing it backwards would let a client
resurrect old notifications indefinitely; "mark unread" is not a feature here.

### GET /api/v1/notifications/count
A cheap badge-only endpoint returning `{ "unread_count": n }`, for the polling path that does not
need the list.

---

## 7. Authorization Matrix Amendment

| Capability | CUSTOMER | AGENT | DISPATCHER | ADMIN |
|---|---|---|---|---|
| Read own notifications | own tickets | assigned | ✓ all | ✓ all |
| Mark own notifications read | ✓ | ✓ | ✓ | ✓ |

There is no cross-principal notification access at all, so there is no deny row to write — the
endpoints take no principal parameter.

---

## 8. Structure

New: `app/models/notification_cursor.py`, `app/repositories/notification_repo.py`,
`app/services/notification_service.py`, `app/schemas/notification.py`,
`app/api/v1/notifications.py`.

The shared scope predicate is extracted from `TicketService.list_tickets` into
`app/repositories/ticket_scope.py` and imported by both. Extraction is required, not optional — a
copied predicate is how INV-17 gets broken six months from now.

---

## 9. Tests

`tests/integration` (`@pytest.mark.db`)
- Cursor upsert moves forward only; a backwards `up_to` is clamped.
- Missing cursor → events since account creation, not all history.
- `SYSTEM`-actor breach events **do** appear (the `IS DISTINCT FROM` NULL case).
- Unread count caps at 99 with 500 qualifying events, with a bounded query.

`tests/api` — INV-17 is an API-level property and gets the weight:
- Customer A is not notified of any event on customer B's ticket.
- **Customer is not notified of an `INTERNAL_NOTE` comment**, and their count does not move.
- Customer *is* notified of a `PUBLIC_REPLY` on their own ticket.
- Agent notified only for assigned tickets; **after being unassigned, previously-visible
  notifications disappear** (the dynamic-scope test).
- Self-authored events excluded for every role.
- Dispatcher/admin see all tickets' events.
- Mark-read zeroes the count; a new event raises it again.
- Parametrized over all four roles in the authorization matrix suite.

---

## 10. Definition of Done

`0015` applies; the scope predicate is shared with ticket listing in one module with no duplicate;
no notification write occurs on any state-change path; the internal-note leak test and the
dynamic-scope test both pass; the badge query is bounded.
