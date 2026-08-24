# spec00.md — F0 Deployment Foundations (Backend)

**Phase 0B · blocking · Owns INV-19 · Source: `rules.md` §1, §3, §4, §5**

Numbered `00` because it runs *before* P13. Everything here is a precondition for deploying the
system at all, and three items are structural — later phases build route handlers and migrations on
top of them.

Frontend counterpart: `cstg-frontend/spec/spec00.md`.

---

## 1. Why This Exists

`rules.md` describes how this stack is deployed. Auditing the codebase against it found six gaps,
two of which are not cosmetic:

- **There is no CORS middleware anywhere** (`grep -rn "add_middleware" app/` returns nothing). The
  application works today only because every fetch is server-side. The first browser-originated
  request to the backend fails.
- **The SLA monitor dies permanently if the database is unreachable at boot** — see §6. Under
  `rules.md §3`'s "start the server even if migrations fail" model, booting against an unreachable
  database is an *expected* state, not an exotic one.

---

## 2. Idempotency (`rules.md §5`)

> Network latency in production often leads users to double-click submission buttons.

The frontend half is already satisfied — all eight forms pass `disabled={pending}`. The backend half
does not exist.

**Most mutations are already safe by construction.** Every state change uses a guarded conditional
UPDATE, so a replayed transition, assignment, or escalation finds the guard false and returns 409.
That is correct behaviour and needs nothing.

**`POST /tickets` is the exception, and it is the worst one.** It is a bare INSERT with no guard, so
a double-click creates two tickets — two SLA clocks, two audit trails, two things for an agent to
work. `POST /tickets/{id}/comments` and attachment upload have the same shape but a lower cost.

### Table

```sql
CREATE TABLE idempotency_key (
  principal_id  uuid        NOT NULL,
  key           text        NOT NULL,
  endpoint      text        NOT NULL,
  request_hash  text        NOT NULL,
  status_code   integer     NOT NULL,
  response_body jsonb       NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, key)
);

CREATE INDEX ix_idempotency_created ON idempotency_key (created_at);
```

Migration `0008_idempotency_key` (see `IMPLEMENTATION_V2.md §3.3` — this claims `0008` and every
later revision shifts down one).

**Keyed on `(principal_id, key)`, not `key` alone.** A client-chosen key is not globally unique and
must never let one principal read another's cached response. This is the same
existence-hiding posture as INV-9.

`request_hash` is a digest of the request body. A replay with the *same* key but a *different* body
is a client bug and returns **422**, not the cached response — silently returning someone's earlier
answer to a different question is worse than an error.

### Semantics

| Case | Behaviour |
|---|---|
| No `Idempotency-Key` header | Normal processing. The header is optional; the endpoint is not weakened for clients that omit it |
| First use of a key | Process, then store status + response body in **the same transaction as the ticket insert** |
| Replay, same body | Return the stored status and body verbatim. No second ticket, no second event |
| Replay, different body | **422** |
| Concurrent replay | The PK arbitrates: the second INSERT conflicts → **409**, "This request is already in progress." |

Storing the response inside the operation's transaction is what makes this correct. A separate write
would leave a window where the ticket exists and the key does not, and the retry creates a duplicate.

Retention: rows older than 24h are pruned. **Not by a new background task** — the complexity freeze
holds and `rules.md` adds no worker. The SLA monitor already wakes every 30s; it prunes on the scan
that crosses each hour boundary. This is a delete of expired rows, not a new subsystem.

### Scope

Applied to `POST /tickets` (required), `POST /tickets/{id}/comments` and
`POST /tickets/{id}/attachments` (same treatment, no extra design). **Not** applied to guarded
operations — they are already idempotent and a key would add a table write to no purpose.

---

## 3. Health (`rules.md §4`)

`GET /health` exists. `GET /health/db` does not, so verification step 2 cannot pass.

```
GET /health      → {"status":"ok"}
GET /health/db   → {"status":"ok","database":"reachable"}       200
                 → {"status":"error","database":"unreachable"}  503
```

Both **unauthenticated** — a load balancer has no token. `/health/db` executes one `SELECT 1` and
must **never** echo the connection string, driver text, or credentials into the response; the reason
goes to logs, a fixed message goes to the client. It is the designated surface for a failed
migration (§5), which is why it returns 503 rather than 200-with-a-flag: an orchestrator has to be
able to act on it.

---

## 4. CORS & Configuration (`rules.md §1`)

`CORSMiddleware` reading `APP_FRONTEND_ORIGIN`:

- **Empty ⇒ no origins allowed.** The default is closed. Under the proxy pattern no browser talks to
  the backend cross-origin, so an empty allow-list is the correct production state; the variable
  exists for the "if external clients connect" case `rules.md §1` anticipates.
- `allow_credentials=False` — authentication is Bearer, not cookies. Enabling it would invite the
  cookie-auth design D3 explicitly declines.

**Database URL normalization.** Railway supplies `postgresql://…`; SQLAlchemy async requires
`postgresql+asyncpg://…`. A `model_validator` in `config.py` upgrades the scheme when the driver is
absent. Without it the failure is a Pydantic error at import — i.e. `rules.md §6`'s "502 from Railway
with no logs", the hardest row in that table to diagnose.

New setting: `frontend_origin: str = ""`.

---

## 5. Migrate on Boot (`rules.md §3`)

The Dockerfile `CMD` is bare uvicorn. It becomes an entrypoint that runs `alembic upgrade head`,
logs the outcome, and **starts the server regardless**:

```sh
alembic upgrade head || echo "MIGRATION FAILED — see /health/db"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Deliberately not `&&`. `rules.md §3` is explicit: a failed migration must be *reachable and
diagnosable* at `/health/db`, not an opaque 502 from a container that never opened a port.

---

## 6. SLA Monitor Boot Resilience — a real bug

In `workers/sla_monitor.py`:

```python
while True:
    async with session_factory() as session:      # ← OUTSIDE the try
        uow = SqlAlchemyUnitOfWork(session)
        try:
            await sla_service.escalate_due_breaches(now())
        except Exception as e:
            logger.error(...)
    await asyncio.sleep(scan_interval_seconds)
```

The inner `try` guards the *scan*. It does not guard **session acquisition**. If the database is
unreachable when the loop iterates, `session_factory()` raises, the exception escapes `while True`,
and the task ends — for the lifetime of the process. Nothing restarts it.

Today this is unlikely, because the app and database start together under compose. Under §5 the app
is *designed* to boot without a working database, which makes it likely.

**The symptom is silence.** The API serves normally. SLA breaches are simply never escalated —
no error, no alert, and the one component that runs with no request behind it is the one whose
failure nothing observes. `main.py` already says this in its logging comment.

Fix: move session acquisition inside the guarded block, so any failure is logged and the loop sleeps
and retries.

---

## 7. Invariants

- **INV-19 (new).** A replayed `Idempotency-Key` from the same principal with the same body creates
  no second resource and returns the original response.
- **INV-5 (preserved).** The idempotency record commits in the same transaction as the operation and
  its audit event.
- **INV-9 (preserved).** Cached responses are scoped to their principal; one principal's key can
  never surface another's response.

---

## 8. Tests

`tests/unit`
- Database URL normalization: `postgresql://` → `postgresql+asyncpg://`; an explicit driver is left
  alone; a non-postgres URL is rejected.
- Request-hash stability across key ordering in the JSON body.

`tests/integration` (`@pytest.mark.db`)
- **INV-19:** `POST /tickets` twice with one key → **one** ticket, one `CREATED` event, identical
  responses.
- Same key, different body → 422.
- Concurrent identical requests → one 201, one 409; exactly one ticket.
- Idempotency row and ticket commit atomically; a forced failure leaves neither.
- Two principals using the same key string get independent results (INV-9).
- Pruning removes rows past 24h and nothing newer.
- **Monitor resilience:** stop the database, advance the loop, restart it — the monitor logs errors
  and resumes escalating. Without the §6 fix this test hangs at "never escalates again".

`tests/api`
- `/health` and `/health/db` unauthenticated; `/health/db` 503 with the database down and leaking no
  connection detail.
- No CORS headers when `APP_FRONTEND_ORIGIN` is unset.

---

## 9. Definition of Done

`0008` applies and reverses; `POST /tickets` is replay-safe with a proven concurrent case; both
health endpoints answer unauthenticated and `/health/db` reports 503 on a dead database without
leaking credentials; CORS is closed by default; a Railway-style `postgresql://` URL boots; a failed
migration yields a reachable 503 rather than a 502; the monitor survives a database restart.
