# spec06.md — F5 Configurable SLA Policy (Versioned)

**Phase P17 · Wave 2 · Owns INV-15 · Preserves INV-1, INV-2 · Depends on P16**

`docs/API.md §11` returns `sla_durations` as a **read-only** reference and says so plainly: *"the
durations live in the domain (SLA_ENGINE.md §1) and are not configurable."* That is right for v1 and
wrong the moment a second customer contract exists. This phase makes durations data without letting
them rewrite history.

---

## 1. Goal

An admin can change what `CRITICAL` means. Tickets created before the change keep the terms they were
created under, and every ticket can say which version of the policy it was judged by.

---

## 2. The Hazard

SLA durations are the terms of a commitment. Making them a mutable table creates two failure modes:

1. **Retroactive breach.** Shorten `HIGH` from 8h to 4h and every open `HIGH` ticket older than 4h
   becomes breached — for a promise nobody made them. Or lengthen it and today's breaches disappear.
2. **Unexplainable history.** A ticket shows a 6-hour window. The config says 8. No one can tell
   whether that is a bug, a past policy, or a pause.

`docs/API.md §11` already solves the first problem for the priority matrix: *"Changing the matrix
affects new tickets only — priority and deadline are frozen at creation (INV-1)."* Because `deadline`
is frozen, changing durations is **already** safe for existing tickets. This spec adds versioning to
solve the second problem: making the frozen number explainable.

---

## 3. Data Model

Immutable versions, one active at a time.

```sql
CREATE TABLE sla_policy_version (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at   timestamptz NOT NULL DEFAULT now(),
  created_by   uuid REFERENCES app_user(id),      -- NULL for the seeded v1 policy
  activated_at timestamptz,
  note         text
);

CREATE TABLE sla_policy_entry (
  version_id uuid NOT NULL REFERENCES sla_policy_version(id) ON DELETE CASCADE,
  priority   priority NOT NULL,
  seconds    integer  NOT NULL CHECK (seconds > 0),
  PRIMARY KEY (version_id, priority)
);

CREATE UNIQUE INDEX ix_sla_policy_active
  ON sla_policy_version ((activated_at IS NOT NULL))
  WHERE activated_at IS NOT NULL AND superseded_at IS NULL;

ALTER TABLE ticket
  ADD COLUMN sla_policy_version_id uuid REFERENCES sla_policy_version(id);
```

A version carries `superseded_at`; activating a new version stamps the old one and clears its place
in the partial unique index, so **exactly one policy is active at any time** and the database says so
rather than the application hoping so.

**Versions and entries are never updated or deleted.** Editing a policy creates a new version. This
is the same reasoning as `ticket_event`: a record that explains a past decision has to still be there
when someone asks. `ON DELETE CASCADE` on entries exists only so an unactivated draft can be
discarded before it is ever used.

**Migration `0014_sla_policy`.** Seeds version 1 from the current domain constants — `CRITICAL` 2h,
`HIGH` 8h, `MEDIUM` 24h, `LOW` 72h — activates it, and backfills every existing ticket's
`sla_policy_version_id` to it. Those are exactly the terms existing tickets were created under, so
the backfill is accurate, not approximate. `NOT NULL` is set after backfill.

---

## 4. Domain

`app/domain/sla.py` currently hardcodes `_DURATION_BY_PRIORITY`. It becomes a parameter:

```python
def duration(priority: Priority, policy: Mapping[Priority, timedelta]) -> timedelta: ...
def deadline_for(
    created_at: datetime, priority: Priority, policy: Mapping[Priority, timedelta]
) -> datetime: ...
```

`domain/` stays pure — it receives the policy, it does not load it. The service reads the active
version through a repository and passes it down, exactly as `priority.resolve` already receives the
priority-rule mapping rather than fetching it.

**Totality**, as with the priority matrix (`docs/SPEC/SLA_ENGINE.md §2`): a policy version must
define all four priorities. A partial policy is a configuration error rejected at write time, never
a runtime default. Reuse the whole-object-submission rule from `docs/API.md §11` — the same reasoning
applies for the same reason.

---

## 5. Creation Flow

`TicketService.create_ticket` gains one step: read the active policy version, use its durations to
compute `deadline`, and stamp `sla_policy_version_id` on the ticket. `sla_due_at` (spec05) is
initialized to the same value.

The read is one indexed row plus four entries, in the same transaction as the insert, so a policy
activated mid-request cannot half-apply.

---

## 6. Activation

`SLAPolicyService.activate(principal, entries, note)`, one transaction:

1. Validate totality and bounds (`0 < seconds <= 90 days`).
2. Insert the new version and its four entries.
3. Stamp `superseded_at` on the current active version.
4. Activate the new one.

Concurrency is handled by the partial unique index, not by a lock: two simultaneous activations mean
one transaction fails on the constraint and returns **409 STATE_CONFLICT**. This is the same "let the
database arbitrate" posture as the guarded conditional UPDATE.

**No effect on existing tickets.** Nothing recomputes any `deadline` or `sla_due_at`. This is worth
asserting in a test rather than trusting, because it is the single thing this feature must never do.

---

## 7. API Contract

`docs/API.md §11` is amended.

### GET /api/v1/configuration
`sla_durations` stops being a static reference and reports the active version:

```json
{
  "priority_rules": [ ... ],
  "sla_policy": {
    "version_id": "uuid", "activated_at": "...", "note": "Q3 enterprise terms",
    "durations": [{ "priority": "CRITICAL", "seconds": 7200 }, ...]
  }
}
```

### PUT /api/v1/configuration/sla-policy — **new**
Auth: required · Authz: `ADMIN`
Request: `{ "durations": [{priority, seconds} × 4], "note": "str?" }` — the **whole** policy, for
the totality reason above. Response 200: the full `ConfigurationResponse`.
Errors: 403; **422** (missing priority, duplicate, non-positive, over the cap); **409** (concurrent
activation).

### GET /api/v1/configuration/sla-policy/history — **new**
Auth: required · Authz: `ADMIN`. Versions newest-first with their entries. This is what makes a
frozen deadline explainable.

`TicketResponse` gains `sla_policy_version_id`. `TicketDetailResponse` embeds the version's durations
so the detail screen can say "2 hours, under the policy active when this was created" without a
second call.

---

## 8. Invariants

- **INV-15 (new).** `ticket.sla_policy_version_id` never changes after creation. Same class of rule
  as INV-1, enforced the same way: no service exposes a write path for it, and no repository method
  includes it in an UPDATE.
- **INV-1, INV-2 (preserved).** `deadline` is still `created_at + duration(priority)` — the duration
  now comes from the ticket's pinned policy version, which is precisely the version that was active
  at `created_at`. The formula is unchanged; its inputs are now recorded.
- **INV-13 (spec05, preserved).** `sla_due_at = deadline + sla_paused_seconds` regardless of policy.

---

## 9. Tests

`tests/unit`
- `duration`/`deadline_for` against an injected policy; totality validation rejects a 3-entry policy,
  a duplicate priority, zero, and negative seconds.

`tests/integration` (`@pytest.mark.db`)
- **The load-bearing test:** create a ticket, activate a policy that halves every duration, assert
  the existing ticket's `deadline`, `sla_due_at`, and `sla_policy_version_id` are all byte-identical
  to before (INV-1, INV-2, INV-15).
- A ticket created after activation uses the new durations and pins the new version.
- Two concurrent activations → one succeeds, one 409; exactly one active version survives.
- The partial unique index rejects a second active version written directly through a session.
- Migration `0014` backfills every pre-existing ticket to the seeded version 1.

`tests/api`
- `PUT /configuration/sla-policy` as ADMIN → 200; as every other role → 403.
- Partial policy → 422.
- History returns versions newest-first with the active one flagged.

---

## 10. Definition of Done

`0013` applies, seeds v1 from the domain constants, and backfills all tickets; the immutability test
in §9 passes; exactly one active version is a database guarantee, not an application convention; the
detail endpoint can explain any ticket's SLA window from its own pinned version.
