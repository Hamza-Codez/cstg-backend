# spec02.md — F1 Conversation: Customer-Authored Replies

**Phase P13 · Wave 1 · Owns INV-11 · Restates INV-9**

Closes the extension named in `docs/SPEC/DOMAIN_MODEL.md §2.4`: *"Customer-authored replies are a v2
extension."* Today a customer can read `PUBLIC_REPLY` comments on their own ticket but cannot answer
one, so every clarification round-trip happens outside the system and the audit trail has a hole in
it.

---

## 1. Goal

A customer may post a `PUBLIC_REPLY` on their own ticket. Staff see it in the same comment list they
already use. `INTERNAL_NOTE` remains staff-only in both directions — a customer can neither write one
nor read one.

Non-goals: editing or deleting a comment (the audit trail is append-only in spirit here too),
attachments on a comment (attachments bind to the ticket — see spec03), and read receipts.

---

## 2. The Schema Problem

`comment.author_id` is `NOT NULL REFERENCES app_user(id)`. Customers live in a different table, so
there is no way to record one as an author.

Two options were considered:

| Option | Shape | Verdict |
|---|---|---|
| Polymorphic pair | `author_type` enum + bare `author_id uuid` with no FK | **Rejected.** Drops referential integrity on the audit path. A deleted or mistyped id becomes an unresolvable author with no database-level complaint. |
| Exclusive-arc FKs | `author_user_id` and `author_customer_id`, both nullable, plus a `CHECK` that exactly one is set | **Chosen.** Keeps both foreign keys real. Mirrors the idiom already in the schema: `ticket_event.system_actor_has_no_id` expresses the same "exactly one shape is valid" rule as a CHECK. |

---

## 3. Data Model

`comment` becomes:

```sql
ALTER TABLE comment ADD COLUMN author_customer_id uuid REFERENCES customer(id);
ALTER TABLE comment ALTER COLUMN author_id DROP NOT NULL;
ALTER TABLE comment RENAME COLUMN author_id TO author_user_id;

ALTER TABLE comment ADD CONSTRAINT comment_exactly_one_author
  CHECK ((author_user_id IS NULL) <> (author_customer_id IS NULL));

-- A customer may only ever author a public reply; an internal note by a
-- customer is not a thing the database will store.
ALTER TABLE comment ADD CONSTRAINT comment_customer_public_only
  CHECK (author_customer_id IS NULL OR type = 'PUBLIC_REPLY');
```

The second CHECK is deliberate belt-and-braces: the service enforces it too, but this is a
visibility rule (INV-9) and the schema is the last line where it cannot be bypassed.

**Migration `0008_comment_customer_author`.** The rename is safe — every existing row is
staff-authored, so `author_user_id` is populated for all of them and `author_customer_id` is NULL,
which already satisfies both CHECKs. Reversible: drop the constraints, rename back, restore
`NOT NULL` (the down path fails loudly if any customer-authored row exists, which is correct — that
data cannot be represented in the old schema and must not be silently discarded).

---

## 4. Domain

No new pure-domain module. Comment visibility is an authorization rule, and
`core/authorization.py` already owns the object-level helpers. Add one:

```python
def authorize_comment_authoring(principal: Principal, ticket: Ticket, type: CommentType) -> None:
    """Who may author what on this ticket.

    A customer may post PUBLIC_REPLY on their own ticket and nothing else. Staff
    authorship is unchanged from v1 (AUTHORIZATION.md §3).
    """
```

The customer path raises `NotFound` — not `Forbidden` — when the ticket is not theirs, consistent
with INV-9 and `docs/API.md §2`.

---

## 5. Service

`CommentService.add_comment` changes shape. Today its first statement is
`if principal.role == Role.CUSTOMER: raise Forbidden(...)`. That guard is replaced by a branch:

| Principal | Allowed type | Object-level rule | Author column |
|---|---|---|---|
| CUSTOMER | `PUBLIC_REPLY` only | `ticket.customer_id == principal.id`, else 404 | `author_customer_id` |
| AGENT | both | assigned only, else 404 | `author_user_id` |
| DISPATCHER / ADMIN | both | any ticket | `author_user_id` |

A customer submitting `type: "INTERNAL_NOTE"` gets **403 FORBIDDEN**, not 404 — the ticket's
existence is not in question, only the capability. This is the one place in the feature where 403 is
the right answer.

**Terminal tickets.** A reply to a `CLOSED` ticket is rejected **422 BUSINESS_RULE_VIOLATION**
("This request is closed."). `RESOLVED` still accepts replies — that is how a customer says "this
isn't actually fixed", and after P16 it is what the reopen flow hangs off.

**Audit.** Unchanged: one `COMMENT` event in the same transaction (INV-5). The event's `actor_type`
becomes `CUSTOMER` for customer replies, and `actor_id` is the customer id — the `ticket_event`
schema already supports this and needs no migration.

---

## 6. Read Scope

`CommentService.list_comments` keeps its existing rule — customers get `PUBLIC_REPLY` only — and it
now returns their own replies as part of that set, which falls out of the type filter with no code
change.

`TicketService._CUSTOMER_VISIBLE_EVENTS` is **not** changed. Comments are read through
`GET /comments`, and surfacing `COMMENT` events in the customer timeline would leak the existence of
internal notes through their timestamps. That reasoning is already recorded in `ticket_service.py`
and still holds.

---

## 7. API Contract

`docs/API.md §7` is amended.

### POST /api/v1/tickets/{id}/comments
Authz becomes: `CUSTOMER` (own ticket, `PUBLIC_REPLY` only), `AGENT` (assigned), `DISPATCHER`,
`ADMIN`. Request body is unchanged.

Errors gain: **403** (customer attempting `INTERNAL_NOTE`), **422** (ticket is `CLOSED`).

### CommentResponse — breaking shape change

```json
{
  "id": "uuid", "ticket_id": "uuid", "type": "PUBLIC_REPLY", "body": "...",
  "created_at": "...",
  "author": { "type": "CUSTOMER", "id": "uuid", "name": "Dana Reed" }
}
```

`author_id` is replaced by a nested `author` object carrying `type` (`CUSTOMER` | `USER`), `id`, and
`name`. Two reasons: a bare id cannot say which table it points into, and the frontend needs the
display name anyway — it currently has no way to render "who wrote this" without a second lookup.

This breaks `CommentResponse` for existing consumers. That is acceptable and intended: the contract
is the drift alarm (`docs/FRONTEND_STRUCTURE.md §6`), and the frontend regenerates types in the same
phase. Do not keep `author_id` alongside `author` as a compatibility shim — one truth per field.

---

## 8. Authorization Matrix Amendment

`docs/AUTHORIZATION.md §3`, the `Add PUBLIC_REPLY` row:

| Capability | CUSTOMER | AGENT | DISPATCHER | ADMIN |
|---|---|---|---|---|
| Add PUBLIC_REPLY | **own** *(was ✗)* | assigned | ✓ | ✓ |
| Add INTERNAL_NOTE | ✗ | assigned | ✓ | ✓ |

Every other row is untouched.

---

## 9. Invariants

- **INV-11 (new).** Exactly one author column is set on every comment. Enforced by
  `comment_exactly_one_author`.
- **INV-9 (restated).** A customer reads only `PUBLIC_REPLY` on their own tickets — unchanged — and
  now may write only that type, on those tickets. The `comment_customer_public_only` CHECK makes the
  write half unbypassable.
- **INV-5.** Still one `COMMENT` event per comment, same transaction.

---

## 10. Tests

`tests/unit`
- `authorize_comment_authoring` truth table across role × ownership × comment type.

`tests/integration` (`@pytest.mark.db`)
- Customer reply inserts with `author_customer_id` set and `author_user_id` NULL; staff comment the
  reverse (INV-11).
- Direct insert violating either CHECK is rejected by the database — both constraints, tested at the
  session level, not through the service.
- Comment + `COMMENT` event commit atomically; forced event-insert failure rolls back the comment.

`tests/api`
- Customer replies to own ticket → 201; author block reports `type: "CUSTOMER"` and the right name.
- Customer replies to another customer's ticket → **404**.
- Customer posts `INTERNAL_NOTE` → **403**.
- Customer replies to a `CLOSED` ticket → **422**; to a `RESOLVED` ticket → **201**.
- Staff comment list on a ticket with mixed authorship returns both, correctly attributed.
- Customer comment list still excludes every `INTERNAL_NOTE` (INV-9 regression).
- Matrix row added to `tests/api/test_authorization_matrix.py`.

---

## 11. Definition of Done

Migration `0008` applies and reverses; both CHECKs proven at the DB level; the four-role matrix is
green; `CommentResponse` regenerates cleanly into the frontend; INV-9 regression tests from v1 still
pass untouched.
