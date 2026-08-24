# spec03.md — F2 Attachments End-to-End

**Phase P14 · Wave 1 · Owns INV-12**

v1 built attachment upload and download but never a way to *list* them, and never wired the feature
to the customer. `docs/AUTHORIZATION.md §3` marks customer upload as `✗ (v2)`; this is that v2. The
feature is also where the storage abstraction promised in `docs/DATABASE.md §3` ("local path in v1;
object-storage key later") stops being a promise.

---

## 1. Goal

Attachments become a complete, usable surface: list them, upload them as a customer at intake or
afterwards, and swap the storage backend without touching a service.

---

## 2. Gaps Being Closed

| Gap | Today |
|---|---|
| No list endpoint | Only `POST /attachments` and `GET /attachments/{aid}` exist. A client that did not just upload a file has no way to learn its id. |
| Customers cannot upload | `AttachmentService.upload_attachment` raises `NotFound` for `Role.CUSTOMER`. |
| No audit event | Uploading writes no `TicketEvent`, so an attachment is the one state change that escapes INV-5. |
| Storage is hardcoded | `UPLOAD_DIR = Path("uploads")` sits as a module constant in the service; the service reads and writes the filesystem directly. |

---

## 3. Storage Port

Introduce `app/core/storage.py` — cross-cutting, like `clock.py`, and for the same reason: a thing
the app depends on that tests must be able to swap.

```python
class StorageBackend(Protocol):
    async def put(self, key: str, source: BinaryIO, max_bytes: int) -> int: ...
    async def open(self, key: str) -> BinaryIO: ...
    async def delete(self, key: str) -> None: ...

class LocalStorage:      # v2 default; wraps the existing streaming writer
class MemoryStorage:     # tests; no disk, no cleanup
```

The existing chunked, cap-enforcing write in `attachment_service._save_upload` moves into
`LocalStorage` **unchanged** — including its `run_in_executor` dispatch. The reason recorded there
still governs: this process also hosts the SLA monitor, so a large upload must never block the event
loop.

`storage_path` keeps its name and becomes a backend-opaque key. For `LocalStorage` it is still
`uploads/{uuid}{ext}`. Selection is by config: `APP_STORAGE_BACKEND=local|memory` (default `local`).

**Why not object storage now.** Adding an S3 client is new infrastructure and a new dependency for
no current requirement. The port is the deliverable; a second implementation ships when someone
actually needs it, which is exactly the trigger `docs/ARCHITECTURE.md §10` asks for.

> **That trigger fires at deployment, not later.** Railway's filesystem is ephemeral, so
> `LocalStorage` loses every uploaded file on redeploy (`IMPLEMENTATION_V2.md` **R14**). Before this
> feature carries real data in production, one of two things must be true: a persistent volume is
> mounted at `uploads/`, or the object-storage backend exists. The decision point is **P22.10** —
> after the first customer upload it becomes a data-loss incident rather than a choice.

---

## 4. Audit Event

Add `EventType.ATTACHMENT`. Uploading writes one, in the upload's transaction, with
`detail = {"attachment_id": ..., "filename": ..., "size": ...}`.

Enum values are native PostgreSQL types, so this needs `ALTER TYPE event_type ADD VALUE
'ATTACHMENT'` in **migration `0009_attachment_event_enum`**, which contains the enum change and
nothing else.

The reason it stands alone: PostgreSQL 16 permits `ADD VALUE` inside a transaction, but the new value
**cannot be used in that same transaction** — and Alembic wraps each migration in one. Nothing in
this phase uses `ATTACHMENT` at migration time, so a combined migration would happen to work here and
then fail in P16, where a CHECK constraint does reference the new value. Keeping enum additions in
their own revision makes the rule uniform instead of a trap that springs once.

`ATTACHMENT` is **not** added to `TicketService._CUSTOMER_VISIBLE_EVENTS`. An attachment a customer
uploaded is visible to them as an attachment; surfacing staff uploads in their timeline would leak
internal activity, the same reasoning that keeps `COMMENT` out.

---

## 5. Authorization — INV-12

The rule is one sentence and it is the whole feature's security model:

> An attachment is readable exactly when its parent ticket is readable, and writable exactly when the
> principal may act on that ticket.

Concretely, both read and write paths call the existing `authorize_ticket_access(principal, ticket)`
and then apply the write rule. There is no attachment-level permission, no share link, no id-guessing
path — an attachment id is meaningless without access to its ticket.

`docs/AUTHORIZATION.md §3` amendment:

| Capability | CUSTOMER | AGENT | DISPATCHER | ADMIN |
|---|---|---|---|---|
| Upload attachment | **own** *(was ✗ v2)* | assigned | ✓ | ✓ |
| List / download attachment | own | assigned | ✓ | ✓ |

**Terminal tickets.** Upload to a `CLOSED` ticket → 422, matching spec02's rule for replies.

---

## 6. Validation

Already implemented and unchanged: `attachment_max_bytes` (10 MB) enforced *while streaming* so an
oversized file never fully lands, and `attachment_allowed_content_types` as an allow-list.

Two additions:

- **Per-ticket count cap.** `APP_ATTACHMENT_MAX_PER_TICKET` (default 20), checked in the service
  before accepting the stream. Without it, "customers may upload" is an unbounded disk write for any
  authenticated account.
- **Declared vs. actual content type.** The client-declared `content_type` is checked against the
  allow-list; the stored value is the declared one. Sniffing the real type is **out of scope** — say
  so here so it is a known limitation rather than an assumed protection. Files are served with
  `Content-Disposition: attachment` and a non-inline disposition, which is what actually matters.

---

## 7. API Contract

`docs/API.md §8` is expanded.

### GET /api/v1/tickets/{id}/attachments — **new**
Auth: required · Authz: same visibility as the parent ticket.
Response 200: `{ "items": [AttachmentResponse] }`. Not paginated — the per-ticket cap bounds it.

### POST /api/v1/tickets/{id}/attachments (multipart)
Authz gains `CUSTOMER` (own ticket).
Errors gain: **422** when the ticket is `CLOSED`, or the per-ticket cap is reached.
Side effects gain: one `ATTACHMENT` event.

### GET /api/v1/tickets/{id}/attachments/{aid}
Unchanged, except that an attachment whose `ticket_id` does not match the `{id}` in the path returns
**404**. Today the pairing is not verified, which makes the nested route lie about its own shape.

`AttachmentResponse` is unchanged and already omits `storage_path` — internal keys never cross the
API boundary.

---

## 8. Invariants

- **INV-12 (new).** Attachment access mirrors parent-ticket access exactly.
- **INV-5 (extended).** Attachment upload now writes its event, closing the one gap where a state
  change had no audit record.

---

## 9. Tests

`tests/integration` (`@pytest.mark.db`)
- Upload + `ATTACHMENT` event commit atomically; forced event failure rolls back the row **and**
  removes the stored object (no orphaned bytes).
- Oversized upload leaves nothing on disk and no row.
- Per-ticket cap rejects the N+1th upload.
- `MemoryStorage` and `LocalStorage` satisfy the same port contract, run as one parametrized suite.

`tests/api`
- Customer uploads to own ticket → 201; to another's → **404**.
- Customer lists attachments on own ticket → 200; agent on an unassigned ticket → **404**.
- Download with a mismatched `{ticket_id}/{attachment_id}` pair → **404**.
- Disallowed content type → 422; `CLOSED` ticket → 422.
- Matrix rows added for upload and list.

---

## 10. Definition of Done

`0009` applies and reverses; the storage port has two implementations passing one shared suite; every
attachment operation is authorized through the parent ticket with no independent path; upload writes
its audit event; the list endpoint is consumed by the frontend in the same phase.
