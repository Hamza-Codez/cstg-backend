# spec10.md — F9 Bulk Dispatch Operations

**Phase P21 · Wave 3 · Owns INV-18 · Depends on P18**

The last phase, and deliberately so: it is a throughput layer over operations that must already be
correct one at a time.

A dispatcher facing 60 unassigned tickets after a weekend has to open, assign, and confirm each one —
180 interactions for one routing decision. The same is true of closing a batch of resolved tickets
and of reassigning an agent's queue when they go on leave.

---

## 1. Goal

Apply assignment, closure, or reassignment to a selected set of tickets in one request, without
weakening any per-ticket guarantee.

---

## 2. The Rule That Shapes Everything — INV-18

> **INV-18.** A bulk operation is a sequence of independent single-ticket transactions. One item's
> failure neither rolls back nor blocks the others, and the response reports the outcome of every
> item individually.

**Not one big transaction.** Wrapping 60 tickets in a single transaction means one 409 — one ticket
someone else touched a second earlier — discards 59 correct assignments. It also holds row locks
across the whole batch while the SLA monitor is trying to escalate, in a process they share.

This is the same posture `SLAService.escalate_due_breaches` already takes, and for the same reason.
Its comment states it directly: *"We must process each candidate in its own transaction block to
ensure that one failure doesn't roll back successful escalations."* Bulk operations are that pattern,
driven by a human instead of a timer.

INV-5 is therefore preserved **per item**: each ticket's state change and its event commit together,
in that ticket's own transaction.

---

## 3. Partial Success Is the Normal Case

A bulk response is never a bare 200 or a bare error. Every item reports its own outcome:

```json
{
  "requested": 60, "succeeded": 57, "failed": 3,
  "results": [
    { "ticket_id": "uuid", "ok": true },
    { "ticket_id": "uuid", "ok": false,
      "error": { "code": "STATE_CONFLICT", "message": "This ticket was just updated by someone else." } },
    { "ticket_id": "uuid", "ok": false,
      "error": { "code": "NOT_FOUND", "message": "Ticket not found." } }
  ]
}
```

**HTTP status is 200 whenever the request itself was well-formed and authorized**, even if every item
failed. The status describes the bulk request; the body describes the items. A 207-style split or a
4xx-on-any-failure would force clients to parse the body anyway while making a wholly successful
request indistinguishable from a partly failed one at the status level.

Per-item errors reuse the taxonomy in `docs/API.md §2` unchanged — the codes a client already knows,
in the envelope shape they already parse. No new error codes.

`results` preserves request order, so a client can zip it against what it sent.

---

## 4. Ordering and Limits

- **`APP_BULK_MAX_ITEMS` (default 100).** Over that → **422** before any work. An unbounded batch is
  an unbounded transaction sequence holding a connection while the monitor needs one.
- **Duplicate ids are rejected 422**, not silently deduplicated. A duplicate means the client built
  its selection wrong, and a silent fix returns a `results` array that does not match the request.
- **Processing is sequential, in request order.** Not concurrent: parallel tasks against one session
  factory would multiply connection use unpredictably and reorder the audit trail for no meaningful
  gain at 100 items.
- **Object-level authorization runs per ticket**, inside its own transaction. A ticket the principal
  cannot see reports `NOT_FOUND` in its result — never a 403 that would confirm existence, and never
  a whole-request failure that would reveal one invisible ticket among ninety-nine visible ones.
  INV-9 applies item by item.

---

## 5. Operations

All three delegate to the **existing** single-ticket services. No bulk-specific business logic
exists; if a bulk path needed its own rule, that rule would be missing from the single path.

### POST /api/v1/tickets/bulk/assignment
Authz: `DISPATCHER`, `ADMIN`
Body: `{ "ticket_ids": [...], "assignee_id": "uuid", "override_capacity": false }`
Per item: `AssignmentService.assign` — target must be an active `AGENT` (INV-8/INV-16), ticket must
be `OPEN`, `IN_PROGRESS`, or `PENDING_CUSTOMER`.

**Capacity is evaluated per item as the batch proceeds**, not once up front. Assigning 60 tickets to
an agent with 10 slots must fill 10 and report 50 × `BUSINESS_RULE_VIOLATION` — a single up-front
check would either pass the whole batch and blow through the ceiling, or fail it all on a limit that
the first ten would not have hit. `override_capacity` applies to the whole batch and is recorded in
each `ASSIGNMENT` event's `detail`.

### POST /api/v1/tickets/bulk/transitions
Authz: `AGENT` (assigned), `DISPATCHER`, `ADMIN`
Body: `{ "ticket_ids": [...], "to": "CLOSED" }`
Per item: `TicketService.transition_ticket` — full legality, role, object-level, and guard checks,
per ticket, unchanged.

**Restricted to `to: "CLOSED"`.** Closing a batch of resolved tickets is housekeeping. Bulk `RESOLVED`
would let an agent mark work done without looking at it — the exact failure the audit trail exists to
make visible — and bulk `IN_PROGRESS` claims work is underway on tickets nobody has opened. This
restriction is a product decision, not a technical one, and belongs in the spec so it is not quietly
relaxed later.

### POST /api/v1/tickets/bulk/reassignment
Authz: `DISPATCHER`, `ADMIN`
Body: `{ "from_assignee_id": "uuid", "to_assignee_id": "uuid", "statuses": ["OPEN","IN_PROGRESS"] }`

The agent-goes-on-leave operation. Selection is server-side by current assignee, so the client does
not have to enumerate ids it may not have paged through yet. Still capped at
`APP_BULK_MAX_ITEMS`; over that → 422 with a message to narrow by status.

This is the operation `docs/API.md §10` gestures at when it says deactivation "only stops **new**
assignments" — until now there was no way to move the old ones.

---

## 6. Structure

`app/services/bulk_service.py` — an orchestrator, not a domain service. It owns iteration, per-item
error capture, and result assembly, and it holds **no** business rules.

Each item gets a fresh session from `request.app.state.session_factory` and its own
`SqlAlchemyUnitOfWork`, exactly as `workers/sla_monitor.py` does per scan iteration. Reusing one
session across items would let a failed item's rolled-back state contaminate the next.

An unexpected exception on one item is caught, logged, and recorded as `INTERNAL_ERROR` in that
item's result. The loop continues — that is INV-18, and it is the one place in the codebase where
swallowing an exception is correct behaviour rather than a smell. It must be commented as such.

`app/api/v1/bulk.py`, registered in `api/v1/router.py`.

---

## 7. Authorization Matrix Amendment

| Capability | CUSTOMER | AGENT | DISPATCHER | ADMIN |
|---|---|---|---|---|
| Bulk assign | ✗ | ✗ | ✓ | ✓ |
| Bulk close | ✗ | assigned | ✓ | ✓ |
| Bulk reassign | ✗ | ✗ | ✓ | ✓ |

Every row mirrors its single-ticket equivalent exactly. A bulk endpoint must never grant a capability
its single-ticket counterpart withholds — that would make batching a privilege-escalation path.

---

## 8. Tests

`tests/integration` (`@pytest.mark.db`)
- **The INV-18 test:** a batch of 10 where item 5 raises unexpectedly — items 1–4 and 6–10 are
  committed, item 5 reports `INTERNAL_ERROR`, and the response accounts for all 10.
- Each successful item wrote exactly one event; failed items wrote none (INV-5 per item).
- Bulk assign to an agent with capacity 10, 30 tickets requested → 10 succeed, 20 report
  `BUSINESS_RULE_VIOLATION`, and the agent's load is exactly 10.
- **Concurrency:** another actor transitions one ticket mid-batch → that item 409s, the rest succeed.
- Bulk reassign moves only tickets in the requested statuses.
- Sessions do not leak: connection count returns to baseline after a 100-item batch.

`tests/api`
- Over `APP_BULK_MAX_ITEMS` → 422; duplicate ids → 422; empty array → 422.
- A batch mixing visible and invisible tickets: invisible ones report `NOT_FOUND` per item, the
  request is still 200, and nothing in the response distinguishes "hidden" from "absent" (INV-9).
- Bulk transition to `RESOLVED` → 422 (restriction is enforced, not just documented).
- Agent bulk-closes a mix of assigned and unassigned tickets → assigned succeed, others `NOT_FOUND`.
- Customer on any bulk endpoint → 403.
- `results` order matches request order.

---

## 9. Definition of Done

Every bulk operation delegates to its single-ticket service with no duplicated rule; one item's
failure leaves the rest committed; per-item results carry the standard error taxonomy; the batch cap
and duplicate rejection are enforced; no bulk endpoint grants anything its single-ticket counterpart
does not; INV-9 holds item by item.
