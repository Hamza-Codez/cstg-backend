# spec05.md — F4 Lifecycle v2: Reopen & SLA Clock Pause

**Phase P16 · Wave 2 · Owns INV-13, INV-14 · Restates INV-3, INV-6 · Preserves INV-1, INV-2**

The deepest change in v2. `docs/REQUIREMENTS.md §11.3` deferred both reopen and clock-pause with the
note *"PROPOSED for v1: strictly-forward state machine, SLA clock runs continuously (no pause).
Alternative: add a 'Waiting on Customer' state that pauses the clock and/or allow reopening."* This
spec takes the alternative.

Read this one before the others in Wave 2. It changes what a ticket's lifecycle *is*, and P17 builds
on its columns.

---

## 1. Why This Is Worth the Risk

Two failures the current model cannot express:

1. **A resolution that did not resolve anything.** A customer replies "still broken" on a `RESOLVED`
   ticket. Today the only recourse is a new ticket, which severs the history and restarts the SLA
   clock on a problem that is hours old. The reported metrics get better as the service gets worse.
2. **Time spent waiting on the customer counts against the agent.** An agent asks for a log file at
   09:00 and gets it at 16:00. Seven hours of a `CRITICAL` ticket's two-hour budget — 3.5× the whole
   budget — burn while the agent can do nothing. Breach becomes a measure of customer response time,
   and the SLA number stops meaning anything.

---

## 2. The Invariant Problem, and the Answer

INV-1 and INV-2 say priority and deadline never change, and `deadline = created_at + duration`. A
pause obviously changes *when a ticket is late*. The naive implementations both fail:

| Approach | Why it fails |
|---|---|
| Mutate `deadline` on resume | Breaks INV-1 and INV-2 outright. Also destroys the audit answer to "what did we promise?" |
| Compute the effective deadline on read | Preserves the invariants but destroys `ix_ticket_sla_scan`. The monitor's candidate query becomes a full scan over an expression involving `now()` — unindexable. `docs/ARCHITECTURE.md §6` names index-served candidate selection as the thing that keeps the monitor cheap. |

**The answer: two columns with two different jobs.**

- `deadline` — the frozen, original promise. Written once at creation. Never recomputed, ever.
  INV-1 and INV-2 survive *verbatim*, not reinterpreted.
- `sla_due_at` — the current effective due time. Materialized, and recomputed at exactly one moment:
  when the clock resumes. Between pauses it is a plain stored timestamp, so it indexes like any
  other and the monitor's query shape does not change at all.

The monitor keeps a single-column partial index; it just points at a different column.

---

## 3. Data Model

```sql
ALTER TYPE ticket_status ADD VALUE 'PENDING_CUSTOMER';

ALTER TABLE ticket
  ADD COLUMN sla_due_at         timestamptz,
  ADD COLUMN sla_paused_at      timestamptz,
  ADD COLUMN sla_paused_seconds integer NOT NULL DEFAULT 0
             CHECK (sla_paused_seconds >= 0),
  ADD COLUMN reopen_count       integer NOT NULL DEFAULT 0
             CHECK (reopen_count >= 0);

UPDATE ticket SET sla_due_at = deadline;          -- backfill: nothing has paused yet
ALTER TABLE ticket ALTER COLUMN sla_due_at SET NOT NULL;

ALTER TABLE ticket ADD CONSTRAINT paused_iff_pending_customer
  CHECK ((sla_paused_at IS NOT NULL) = (status = 'PENDING_CUSTOMER'));

DROP INDEX ix_ticket_sla_scan;
CREATE INDEX ix_ticket_sla_scan ON ticket (sla_due_at)
  WHERE sla_breached_at IS NULL
    AND status NOT IN ('RESOLVED','CLOSED','PENDING_CUSTOMER');
```

The `breached_only_when_past_deadline` CHECK must be **redefined against `sla_due_at`**, not
`deadline` — a ticket that paused then breached has `sla_breached_at >= sla_due_at > deadline`, which
still satisfies the old constraint, but the constraint is now checking the wrong thing. Rewrite it
rather than leave it accidentally-passing.

`PENDING_CUSTOMER` is excluded from the index predicate as well as the query: a paused ticket is
never a breach candidate, so it should not occupy the index at all.

**Two migrations, not one.**

| Rev | Contents |
|---|---|
| `0012_pending_customer_enum` | `ALTER TYPE ticket_status ADD VALUE 'PENDING_CUSTOMER'` — nothing else |
| `0013_lifecycle_v2` | The columns, the backfill, both CHECKs, and the index swap |

This split is mandatory, not stylistic. PostgreSQL 16 allows `ADD VALUE` inside a transaction but
forbids **using** the new value in that same transaction, and Alembic wraps each migration in one.
`paused_iff_pending_customer` uses the literal `'PENDING_CUSTOMER'`, so a single combined migration
fails at runtime — not at review, and not in a way the autogenerate diff would reveal.

Both are reversible, with the same honest failure mode: `0013`'s down path must refuse to run if any
ticket is `PENDING_CUSTOMER` or has `reopen_count > 0`, because that state has no representation in
the v1 schema.

Also in `0013`: the existing `breached_only_when_past_deadline` CHECK is **redefined against
`sla_due_at`**. Left alone it still passes — a paused ticket breaches at `sla_due_at`, which is later
than `deadline` — so it would silently go on checking the wrong column.

---

## 4. Transition Table

`docs/SPEC/TICKET_LIFECYCLE.md §2` grows from three rows to six.

| # | From | To | Trigger | Authorized roles | Guard | Side effects |
|---|---|---|---|---|---|---|
| T1 | OPEN | IN_PROGRESS | start | assigned AGENT, ADMIN | `assignee_id IS NOT NULL` | STATUS_CHANGE |
| T2 | IN_PROGRESS | RESOLVED | resolve | assigned AGENT, ADMIN | — | set `resolved_at`; STATUS_CHANGE |
| T3 | RESOLVED | CLOSED | close | assigned AGENT, DISPATCHER, ADMIN | — | STATUS_CHANGE |
| **T4** | IN_PROGRESS | PENDING_CUSTOMER | await customer | assigned AGENT, ADMIN | — | set `sla_paused_at = now`; STATUS_CHANGE |
| **T5** | PENDING_CUSTOMER | IN_PROGRESS | resume | assigned AGENT, ADMIN, **or the owning CUSTOMER** | — | accrue pause; recompute `sla_due_at`; clear `sla_paused_at`; STATUS_CHANGE |
| **T6** | RESOLVED | IN_PROGRESS | reopen | assigned AGENT, DISPATCHER, ADMIN, **or the owning CUSTOMER** | within `APP_REOPEN_WINDOW_DAYS` of `resolved_at` | clear `resolved_at`; `reopen_count += 1`; STATUS_CHANGE |

Two rows break a v1 rule on purpose, and it is the same rule both times: **`docs/AUTHORIZATION.md §3`
says a customer drives no transition.** T5 and T6 make them drive two.

This is justified and narrow. Both are the customer answering a question the system asked them:
"we're waiting on you" and "we think this is fixed". Nobody else can answer either. The customer
still cannot start, resolve, or close — the transitions that assert work was done remain staff-only,
which is what the original rule was protecting.

**T5 auto-resume.** A customer reply (spec02) on a `PENDING_CUSTOMER` ticket performs T5
automatically, in the reply's transaction. That is the whole point of the state: the clock restarts
when the customer responds, not when an agent notices they responded. `CommentService` calls
`TicketService` for this rather than touching ticket columns itself — the transition, its guard, and
its event stay in one place.

**T6 window.** Reopen is bounded by `APP_REOPEN_WINDOW_DAYS` (default 14) from `resolved_at`. Past
the window, or once `CLOSED`, the answer is 422 with a message that says to open a new request.
Unbounded reopen would let a year-old ticket return and immediately breach against a year-old
deadline.

`CLOSED` remains terminal. There is exactly one way out of `RESOLVED` besides closing, and no way out
of `CLOSED`.

---

## 5. Pause Arithmetic

Pure, in `app/domain/sla.py`, no I/O:

```python
def accrue_pause(paused_at: datetime, now: datetime, paused_seconds: int) -> int:
    """Total paused seconds after a pause that began at `paused_at` ends at `now`."""


def due_at(deadline: datetime, paused_seconds: int) -> datetime:
    """The effective due time. INV-13: sla_due_at == deadline + paused_seconds."""
```

`due_at` is deliberately trivial and deliberately exists: it is the single definition of INV-13, so
the service, the backfill, and the tests all compute it the same way.

**While paused**, `sla_due_at` is stale — it still holds the pre-pause value. That is correct and
harmless: the index predicate excludes `PENDING_CUSTOMER`, so nothing reads it until T5 rewrites it.
The invariant is stated as "whenever the clock is not paused" for exactly this reason.

**Breach detection** is otherwise untouched. `is_breach` compares against `sla_due_at` instead of
`deadline`, and `PENDING_CUSTOMER` joins `RESOLVED`/`CLOSED` in the non-breaching set.

**SLA met** becomes `resolved_at <= sla_due_at`. A ticket that paused legitimately gets the time
back; the `<=` boundary from `docs/SPEC/SLA_ENGINE.md §4` is unchanged.

---

## 6. Reopen and Breach — INV-6 Holds

A reopened ticket **keeps `sla_breached_at` and keeps `escalation_level`.**

`sla_breached_at IS NULL` remains the idempotency guard, so a ticket that already breached cannot
breach again no matter how many times it reopens. INV-6 — "`sla_breached_at` goes null→ts exactly
once" — is preserved literally, with no special-casing in the monitor.

The alternative (clear the breach on reopen, let it breach afresh) was rejected: it lets a service
erase its own failures by reopening, and it makes `escalation_level` a number that can go down.
`reopen_count` carries the reopen signal instead, and the `STATUS_CHANGE` events carry the history.

A ticket reopened *before* its due time behaves entirely normally — it can still breach for the first
time, because its guard was never tripped.

---

## 7. Service & Repository

`TicketRepository.transition_if` gains optional column writes so a transition can set
`sla_paused_at`, `sla_due_at`, `resolved_at`, and increment `reopen_count` **inside the same guarded
UPDATE**. This matters: if the resume and the `sla_due_at` recomputation were two statements, a
concurrent transition between them would leave the clock wrong. One statement, one guard, one
outcome.

```sql
UPDATE ticket
   SET status = :to, updated_at = :now,
       sla_paused_at = NULL,
       sla_paused_seconds = :accrued,
       sla_due_at = :recomputed
 WHERE id = :id AND status = 'PENDING_CUSTOMER';   -- rowcount 0 ⇒ 409
```

`TicketService.transition_ticket` keeps its existing order — legality, then role, then object-level
narrowing, then guards — and gains the T4/T5/T6 arms. The customer branches are the first place
`principal.type == CUSTOMER` reaches the transition path, so the object-level check
(`ticket.customer_id == principal.id`, else 404) must be explicit there.

---

## 8. API Contract

`docs/API.md §5`: `TransitionRequest.to` accepts `PENDING_CUSTOMER` in addition to the existing
three. Reopen is `{"to": "IN_PROGRESS"}` from `RESOLVED` — no separate endpoint, because it is a row
in the transition table like every other move.

`TicketResponse` gains `sla_due_at`, `sla_paused_at`, `sla_paused_seconds`, `reopen_count`, and keeps
`deadline`.

**The frontend countdown must switch to `sla_due_at`.** `deadline` stays in the response as the
audit record of the original promise — it is what "we promised 2 hours" means — but it is no longer
the number a countdown counts to. Both fields being present is exactly why they must be named for
their jobs.

New errors: **422** when reopening outside the window or from `CLOSED`.

---

## 9. Authorization Matrix Amendment

| Capability | CUSTOMER | AGENT | DISPATCHER | ADMIN |
|---|---|---|---|---|
| T4 IN_PROGRESS→PENDING_CUSTOMER | ✗ | assigned | ✗ | ✓ |
| T5 PENDING_CUSTOMER→IN_PROGRESS | **own** | assigned | ✗ | ✓ |
| T6 RESOLVED→IN_PROGRESS (reopen) | **own** | assigned | ✓ | ✓ |

The note under §3 of `docs/AUTHORIZATION.md` — "CUSTOMER may drive no transition at all" — must be
rewritten, not left to contradict the table. The replacement rule: *a customer may drive only
transitions that answer a question addressed to them, and never one that asserts work was done.*

---

## 10. Invariants

- **INV-13 (new).** `sla_due_at = deadline + sla_paused_seconds` whenever not paused.
- **INV-14 (new).** `sla_paused_at IS NOT NULL` iff `status = PENDING_CUSTOMER`. DB-enforced by
  `paused_iff_pending_customer`.
- **INV-1, INV-2 (preserved verbatim).** `priority` and `deadline` are still written once and never
  recomputed.
- **INV-3 (restated).** Legal transitions are those in the table — now T1–T6.
- **INV-6, INV-7 (preserved).** At-most-once breach via `sla_breached_at IS NULL`; terminal tickets
  never escalate, and `PENDING_CUSTOMER` joins them in the monitor's exclusion set.

---

## 11. Tests

`tests/unit`
- `due_at` and `accrue_pause` arithmetic, including a paused-then-resumed-then-paused sequence.
- T1–T6 legal; every other pair illegal (the full 5×5 status matrix).
- Role authorization per transition, including the two customer-allowed rows.
- `is_breach` returns False for `PENDING_CUSTOMER` regardless of how far past due.

`tests/integration` (`@pytest.mark.db`)
- **INV-13 after each transition** — assert the identity holds, computed independently of the code
  that wrote it.
- Pause 3h on a `CRITICAL` (2h) ticket, resume, advance the frozen clock: breach fires 3h later than
  it would have, and not before.
- A ticket paused past its original deadline is **not** escalated by the monitor (INV-7 extension) —
  the key test that the index predicate and the query agree.
- Reopen keeps `sla_breached_at` and `escalation_level`; a second scan after reopen writes no second
  breach event (INV-6).
- Reopen before due time can still breach for the first time.
- **Concurrency:** customer reply auto-resume (T5) races an agent's manual T5 — guarded UPDATE, one
  wins, the other 409, and `sla_paused_seconds` is accrued exactly once.
- **Concurrency:** monitor escalation races T4 pause — no escalation of a ticket that became
  `PENDING_CUSTOMER` first.
- `paused_iff_pending_customer` rejected at the DB level from a direct session write.

`tests/api`
- Customer resumes own paused ticket → 200; another customer's → 404.
- Customer attempts T1/T2/T3 → still 403 (the v1 rule, unbroken).
- Reopen inside window → 200; outside → 422; from `CLOSED` → 422.
- Dispatcher reopens (allowed) but cannot pause → 403.

---

## 12. Definition of Done

`0012` applies, backfills, and refuses to reverse over v2-only data; INV-13/INV-14 hold after every
transition in an integration test that recomputes them independently; the monitor never escalates a
paused ticket; INV-6 survives reopen; both concurrency races pass; the v1 customer-cannot-transition
tests still pass for T1–T3.
