# spec07.md — F6 Assignment Automation: Claim, Round-Robin & Capacity

**Phase P18 · Wave 2 · Owns INV-16 · Strengthens INV-8**

`docs/REQUIREMENTS.md §11.4` deferred this by name: *"PROPOSED: manual dispatch for v1. Alternative:
auto round-robin."* Manual dispatch means every ticket waits for a human to route it — including the
`CRITICAL` outage that arrives at 02:00 with a two-hour budget and no dispatcher awake to see it.

---

## 1. Goal

Three additions, smallest to largest:

1. **Claim** — an agent takes an unassigned ticket themselves, without waiting for a dispatcher.
2. **Capacity** — an agent has a workload ceiling that assignment respects.
3. **Auto-assign** — a ticket with no assignee is routed automatically by a configurable strategy.

Manual dispatch is not replaced. It stays the default and remains available on every ticket; these
are the paths that run when nobody is dispatching.

---

## 2. Data Model

```sql
ALTER TABLE app_user
  ADD COLUMN max_open_tickets integer CHECK (max_open_tickets IS NULL OR max_open_tickets > 0),
  ADD COLUMN accepts_auto_assignment boolean NOT NULL DEFAULT true;

CREATE TABLE assignment_config (
  id                 boolean PRIMARY KEY DEFAULT true CHECK (id),
  strategy           text NOT NULL DEFAULT 'MANUAL'
                     CHECK (strategy IN ('MANUAL','ROUND_ROBIN','LEAST_LOADED')),
  auto_assign_on_create boolean NOT NULL DEFAULT false,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  updated_by         uuid REFERENCES app_user(id)
);

CREATE INDEX ix_ticket_open_load ON ticket (assignee_id)
  WHERE status IN ('OPEN','IN_PROGRESS','PENDING_CUSTOMER');
```

`max_open_tickets IS NULL` means no ceiling — the v1 behaviour, so the migration changes nothing for
existing staff. `accepts_auto_assignment` lets an agent stay assignable by a dispatcher while being
skipped by automation (part-time, on another rotation, on leave).

`assignment_config` is a **singleton table**: a boolean primary key with `CHECK (id)` admits exactly
one row. This is deliberate over a settings blob — the columns get real types and real constraints,
and there is no code path that creates a second config.

`ix_ticket_open_load` serves the load count. Its predicate includes `PENDING_CUSTOMER` (spec05): a
ticket waiting on a customer still belongs to the agent and still occupies a slot, even though its
clock is stopped.

**Migration `0015_assignment_automation`.** Seeds one `assignment_config` row with `MANUAL` /
`auto_assign_on_create = false`, so behaviour after the migration is byte-identical to before it.
Automation is opt-in.

---

## 3. Domain — Pure Selection

`app/domain/assignment.py`. No I/O; it receives candidates and returns a choice.

```python
@dataclass(frozen=True)
class Candidate:
    user_id: UUID
    open_tickets: int
    max_open_tickets: int | None
    last_assigned_at: datetime | None


def has_capacity(c: Candidate) -> bool: ...
def select(candidates: Sequence[Candidate], strategy: Strategy) -> UUID | None: ...
```

| Strategy | Rule | Tie-break |
|---|---|---|
| `MANUAL` | Never selects. `select` returns `None`. | — |
| `ROUND_ROBIN` | Least-recently-assigned with capacity. | `user_id` ascending |
| `LEAST_LOADED` | Fewest open tickets, capacity respected. | Then least-recently-assigned, then `user_id` |

`None` — "no eligible agent" — is a first-class result, not an error. Every caller must handle it,
and the answer is always the same: **leave the ticket unassigned**. A ticket with no agent is a
dispatcher's problem, which is a visible, recoverable state. Forcing an over-capacity assignment
would hide it.

Ties break on `user_id` rather than arbitrarily so the function is deterministic and unit-testable —
the same reasoning that makes `priority.resolve` a total mapping.

`last_assigned_at` is derived from the most recent `ASSIGNMENT` event naming the agent, so
round-robin state lives in the audit log that already records it. **Why not a counter column:** a
counter is a second source of truth that can drift from the events, and it needs its own concurrency
story. The events are already append-only and already correct.

---

## 4. Service

### Claim

`AssignmentService.claim(principal, ticket_id)` — an agent assigns a ticket to themselves.

Guarded so two agents cannot claim the same ticket:

```sql
UPDATE ticket SET assignee_id = :me, updated_at = :now
 WHERE id = :id
   AND assignee_id IS NULL                       -- unclaimed only
   AND status IN ('OPEN','IN_PROGRESS');         -- same window as manual assignment
-- rowcount 0 ⇒ 409 STATE_CONFLICT ("already claimed")
```

Claiming is **taking**, not reassigning: the guard requires `assignee_id IS NULL`. An agent cannot
claim a colleague's ticket — that is a dispatcher action, and the 409 says so.

Capacity applies to claim too, but as **422**, not a silent refusal: "You're at your ticket limit."
An agent deliberately taking work deserves to know why it was refused.

Writes an `ASSIGNMENT` event with `actor_id = principal.id`, one transaction (INV-5).

### Auto-assign

`AssignmentService.auto_assign(ticket_id)` — build candidates, call `domain.select`, apply through
the **existing** `assign_if_open_or_in_progress` guarded update, write the `ASSIGNMENT` event with
`actor_type = SYSTEM`, `actor_id = NULL`. The `system_actor_has_no_id` CHECK already covers this
shape; automated assignment is the second `SYSTEM` actor in the system after the SLA monitor.

Called from `TicketService.create_ticket` when `auto_assign_on_create` is on — **in the same
transaction as the ticket insert**, so a ticket is never briefly visible unassigned, and a failure to
assign never leaves a half-created ticket.

If `select` returns `None`, the ticket is created unassigned and no `ASSIGNMENT` event is written.
That is a normal outcome, not an error, and it must not fail the creation.

**No background sweeper.** Auto-assignment happens at creation and on explicit request only. A
periodic re-assignment task would be a second background worker, which `docs/ARCHITECTURE.md §10`
freezes out, and it would fight the dispatcher for control of the queue.

---

## 5. INV-16 — Automation Cannot Escape INV-8

INV-8 says a ticket is assigned only to an active `AGENT`. That guarantee currently lives in
`AssignmentService`'s manual path. Automation adds a second path to the same column, so the rule is
restated to cover both:

> **INV-16.** Every assignment — manual, claimed, or automatic — targets an active user whose role is
> `AGENT`.

Enforced by construction: candidate selection queries `role = 'AGENT' AND is_active` and the
`Candidate` type carries no way to represent anyone else. The final write goes through the same
repository method the manual path uses, so there is one place where `assignee_id` changes.

A **deactivated agent keeps their tickets** — `docs/API.md §10` is explicit that deactivation "only
stops new assignments". Automation must not reassign their open work; that is a dispatcher decision,
and bulk reassignment is spec10's job.

---

## 6. API Contract

### POST /api/v1/tickets/{id}/claim — **new**
Auth: required · Authz: `AGENT`, `ADMIN`
Response 200: updated `TicketResponse`.
Errors: 403; 404 (ticket); **409** (already assigned); **422** (at capacity).

### POST /api/v1/tickets/{id}/assignment
Unchanged, plus: **422** when the target agent is at capacity. A dispatcher may override with
`{"assignee_id": "...", "override_capacity": true}` — capacity is a routing heuristic, not an
invariant, and a dispatcher handling a `CRITICAL` outage must be able to say so. The override is
recorded in the `ASSIGNMENT` event's `detail`.

### GET /api/v1/configuration → gains
```json
"assignment": { "strategy": "ROUND_ROBIN", "auto_assign_on_create": true }
```

### PUT /api/v1/configuration/assignment — **new**
Auth: required · Authz: `ADMIN`. Sets strategy and the auto-assign flag.

### PATCH /api/v1/users/{id}
Gains `max_open_tickets` and `accepts_auto_assignment` alongside `is_active`. Authz unchanged
(`ADMIN`).

### GET /api/v1/users
`UserSummary` gains `open_ticket_count`, `max_open_tickets`, and `accepts_auto_assignment`, so the
assign dialog can show load and grey out full agents. Still `DISPATCHER`/`ADMIN` only — agents have
no reason to enumerate staff (`docs/API.md §10`).

---

## 7. Authorization Matrix Amendment

| Capability | CUSTOMER | AGENT | DISPATCHER | ADMIN |
|---|---|---|---|---|
| Claim unassigned ticket | ✗ | **✓ (unassigned only)** | ✗ | ✓ |
| Override capacity on assign | ✗ | ✗ | ✓ | ✓ |
| Configure assignment strategy | ✗ | ✗ | ✗ | ✓ |
| Set agent capacity | ✗ | ✗ | ✗ | ✓ |

Dispatchers do not claim — they assign, which they can already do to anyone including themselves if
they hold `AGENT`. Adding claim for them would be a second spelling of an existing capability.

---

## 8. Tests

`tests/unit`
- `select` for each strategy: capacity respected, `None` when everyone is full, deterministic
  tie-breaks, `MANUAL` always `None`, empty candidate list → `None`.
- `has_capacity` with `max_open_tickets = None` (uncapped) and at exactly the ceiling.

`tests/integration` (`@pytest.mark.db`)
- **Concurrency:** two agents claim the same ticket simultaneously → one 200, one 409, one
  `ASSIGNMENT` event.
- Round-robin across three agents distributes 1-2-3-1-2-3, driven by real `ASSIGNMENT` events.
- Auto-assign on create commits ticket + `CREATED` + `ASSIGNMENT` in one transaction; a rollback
  leaves none of them.
- No eligible agent → ticket created unassigned, no `ASSIGNMENT` event, request succeeds.
- Inactive and `accepts_auto_assignment = false` agents are never selected (INV-16).
- Deactivating an agent leaves their existing tickets assigned.
- `PENDING_CUSTOMER` tickets count toward load.

`tests/api`
- Agent claims unassigned → 200; claims an assigned ticket → 409; at capacity → 422.
- Customer/dispatcher claim → 403.
- Dispatcher assigns over capacity without override → 422; with override → 200, and `detail` records
  it.
- Non-admin `PUT /configuration/assignment` → 403.

---

## 9. Definition of Done

`0014` applies with automation off, so behaviour is unchanged until an admin turns it on; the claim
race passes; every assignment path funnels through one guarded repository write; INV-16 is proven for
manual, claimed, and automatic assignment; round-robin state is derived from `ticket_event` with no
counter column.
