# spec01.md — V2 Program Index (Backend)

Entry point for the v2 backend specification. v1 (phases P0–P12 of `docs/IMPLEMENTATION.md`) is
complete and is the baseline; nothing here rewrites it. Every document below specifies a *delta*
against the shipped system and names the invariants it establishes or restates.

This file is an index and a program plan. It contains no feature detail — that lives in the
per-feature specs listed in §2.

---

## 1. Scope & Constraints

**The complexity freeze holds.** `docs/ARCHITECTURE.md §10` is unamended: no message broker, no
Redis, no cache layer, no websockets, no second deployable, no LLM. Search is PostgreSQL full-text.
Notifications are a read model over `ticket_event` polled by the client. `docker compose` still
brings up exactly `api` + `db`, and the SLA monitor remains the only background task in the process.

Any feature below that appeared to need new infrastructure has been designed so it does not. Where
that design choice is load-bearing, the feature spec says so explicitly under "Why not X".

**What v2 adds.** Three things the v1 docs deferred by name — customer-authored replies
(`docs/SPEC/DOMAIN_MODEL.md §2.4`), customer attachment upload (`docs/AUTHORIZATION.md §3`), and
lifecycle reopen/pause (`docs/REQUIREMENTS.md §11.3`) — plus the operational features a support desk
needs once it has real volume: search, assignment automation, notifications, deeper reporting, and
bulk dispatch.

---

## 2. Feature Specs

| # | File | Feature | Phase | Wave |
|---|---|---|---|---|
| F0 | [spec00.md](spec00.md) | Deployment foundations — idempotency, health, CORS, boot resilience | **0B** | — |
| F1 | [spec02.md](spec02.md) | Conversation — customer-authored replies | P13 | 1 |
| F2 | [spec03.md](spec03.md) | Attachments end-to-end + customer upload | P14 | 1 |
| F3 | [spec04.md](spec04.md) | Search, filters & saved views | P15 | 1 |
| F4 | [spec05.md](spec05.md) | Lifecycle v2 — reopen & SLA clock pause | P16 | 2 |
| F5 | [spec06.md](spec06.md) | Configurable SLA policy (versioned) | P17 | 2 |
| F6 | [spec07.md](spec07.md) | Assignment automation — claim, round-robin, capacity | P18 | 2 |
| F7 | [spec08.md](spec08.md) | Notifications & unread tracking | P19 | 3 |
| F8 | [spec09.md](spec09.md) | Metrics v2 — time series, per-agent, export | P20 | 3 |
| F9 | [spec10.md](spec10.md) | Bulk dispatch operations | P21 | 3 |

Frontend counterparts carry the **same numbers** in `cstg-frontend/spec/`, so `spec04.md` is search
on both sides.

---

## 3. Waves & Sequencing

**Wave 1 — the documented deferrals (P13–P15).** Low invariant risk. Each closes a gap the v1 docs
already named, or a specced-but-unbuilt surface. None of them touch the state machine or the SLA
engine, so they can ship in any order and in parallel.

**Wave 2 — domain expansion (P16–P18).** The invariant-sensitive work. P16 changes what a ticket's
lifecycle *is*; P17 changes where SLA durations come from. Both touch `ticket` SLA columns, so they
are strictly ordered P16 → P17 to keep migrations linear. P18 depends on neither and may run beside
them.

**Wave 3 — operational scale (P19–P21).** Read-heavy features that layer on everything before them.
P19 surfaces the events P13 and P16 introduce, so it comes after both.

```
P13 (conversation) ─┐
P14 (attachments)  ─┼─▶ P19 (notifications) ─┐
P15 (search)       ─┘                        │
                                             ├─▶ P21 (bulk ops)
P16 (lifecycle) ─▶ P17 (sla policy)          │
P18 (assignment) ───────────────────────────┘
                   P20 (metrics v2) — depends only on P16
```

A phase does not start until the previous wave's invariants are green. This is the same rule as
`docs/IMPLEMENTATION.md §1`.

**The parallelism above is logical, not operational.** The graph shows which phases have no
dependency on each other; it does not license running two of them at once. Alembic revisions form a
single linear chain, so two phases holding unmerged migrations would make `down_revision` depend on
merge order. `IMPLEMENTATION_V2.md §3.3` is the authoritative rule: phases execute sequentially, at
most one unmerged migration exists at a time, and only a phase's *frontend* work may overlap the
next phase's backend work.

---

## 4. Invariant Register — v2 Additions

`docs/SPEC/DOMAIN_MODEL.md §4` holds INV-1 … INV-10 and remains authoritative for them. v2 appends:

| ID | Invariant | Established in | Owner |
|---|---|---|---|
| INV-11 | A comment has exactly one author: `author_user_id` XOR `author_customer_id` is non-null. | P13 | spec02 |
| INV-12 | An attachment is readable exactly when its parent ticket is readable — no independent access path. | P14 | spec03 |
| INV-13 | `sla_due_at = deadline + sla_paused_seconds` whenever the clock is not paused. | P16 | spec05 |
| INV-14 | `sla_paused_at IS NOT NULL` if and only if `status = PENDING_CUSTOMER`. | P16 | spec05 |
| INV-15 | A ticket's `sla_policy_version_id` never changes after creation. | P17 | spec06 |
| INV-16 | Auto-assignment selects only active users whose role is AGENT (strengthens INV-8 to cover the automatic path). | P18 | spec07 |
| INV-17 | A notification never reveals an event on a ticket its recipient may not read. | P19 | spec08 |
| INV-18 | A bulk operation is a sequence of independent single-ticket transactions; one item's failure neither rolls back nor blocks the others. | P21 | spec10 |
| INV-19 | A replayed `Idempotency-Key` from the same principal with the same body creates no second resource and returns the original response. | 0B | spec00 |

### Invariants that are restated, not broken

Three v1 invariants come under pressure in Wave 2. None of them are weakened:

- **INV-1 / INV-2 (priority & deadline frozen).** P16 introduces a pause, but the `deadline` column
  is still written once at creation and never recomputed. Pausing accumulates into a *separate*
  column and the monitor reads a *separate* materialized column. `deadline` remains the frozen
  record of the terms the ticket was created under. See spec05 §3.
- **INV-3 (only T1–T3 are legal).** P16 extends the transition table to T1–T6. The invariant's
  wording becomes "only transitions in the table are legal"; the mechanism — a total table plus a
  guarded conditional UPDATE — is unchanged.
- **INV-6 (escalate at most once).** A reopened ticket keeps its `sla_breached_at`, so it cannot
  breach twice. Reopening is counted in `reopen_count`, not by clearing the breach. See spec05 §4.

---

## 5. Rules Every Feature Spec Obeys

These are the v1 rules that v2 inherits without restating them in each file:

1. **Layering.** `api → services → repositories → models`; `domain/` is pure and imports nothing but
   enums. No new layer, no new top-level package (`docs/BACKEND_STRUCTURE.md §2`).
2. **One transaction per state change, including its audit event** (INV-5). New write operations add
   their own `EventType` or reuse an existing one — none of them skip the event.
3. **Guarded conditional UPDATEs** for every state change, never read-then-write. New operations that
   race (claim, pause, bulk close) each specify their guard predicate.
4. **Errors map by exception type** to the taxonomy in `docs/API.md §2`. v2 adds no new error codes;
   anything new fits an existing row.
5. **Authorization is role-level AND object-level**, and hidden resources return 404 (INV-9).
   `docs/AUTHORIZATION.md §3` remains the single source of truth — every feature spec below states
   its matrix rows as an amendment to that table.
6. **Alembic only.** The head at the start of v2 is `0007_column_defaults`. **The migration ledger in
   `IMPLEMENTATION_V2.md §3.3` is the sole allocation authority** — numeric prefixes written in these
   feature specs are indicative and are overridden by it. Refer to a migration by its purpose suffix
   (`_lifecycle_v2`, `_sla_policy`), which is stable under renumbering. `docs/DATABASE.md §7`
   numbering resumes (the hash-named `99de55862c0e` sits between `0006` and `0007` and is left alone).
7. **Contract regenerates.** Every schema change reaches the frontend through
   `npm run gen:api`; the frontend never hand-writes a type.

---

## 6. Testing Posture

`docs/TESTING.md` layers are unchanged: `tests/unit` (pure domain, no DB), `tests/integration`
(services + real DB, marked `@pytest.mark.db`), `tests/api` (routers, auth, authz).

Each feature spec ends with a test list mapped to invariant IDs. Two categories are mandatory
wherever they apply and are called out per spec rather than assumed:

- **Concurrency.** Any new guarded UPDATE gets an interleaving test in `tests/integration`, in the
  style of the existing SLA race test.
- **Authorization matrix.** Any new capability is added to the parametrized matrix in
  `tests/api/test_authorization_matrix.py` for all four roles, including the deny cases.

---

## 7. Definition of Done

Unchanged from `docs/IMPLEMENTATION.md §6`, with one addition: **the v2 spec file itself is updated**
when implementation reveals the design was wrong. A spec that disagrees with shipped code is worse
than no spec, and the drift-control rule in `docs/AUTHORIZATION.md §3` applies to these documents
too.
