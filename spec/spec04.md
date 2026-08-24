# spec04.md — F3 Search, Filters & Saved Views

**Phase P15 · Wave 1 · Owns no new invariant · Must not weaken INV-9**

`docs/UIUX_FRONTEND.md §5` specifies a global search in the staff top bar and `§7.3.4` a filtered
"All tickets" view. Neither exists. Today the only way to find a ticket is to page through a list
ordered by creation time, which stops working at the first few hundred tickets.

---

## 1. Goal

Find a ticket by what it says, not just when it arrived; filter a queue down the axes dispatchers
actually work in; and let staff keep a filter combination they use daily.

---

## 2. Search Design

**PostgreSQL full-text.** `tsvector` over `subject` and `body`, GIN-indexed, ranked with
`ts_rank_cd`. No external search service — that would be new infrastructure with no requirement
behind it, and at this tier Postgres FTS is not a compromise.

A **generated column** keeps the vector in step with no trigger and no application code:

```sql
ALTER TABLE ticket ADD COLUMN search_vector tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(subject,'')), 'A') ||
    setweight(to_tsvector('english', coalesce(body,'')),    'B')
  ) STORED;

CREATE INDEX ix_ticket_search ON ticket USING GIN (search_vector);
```

Subject is weighted above body so a title match outranks a passing mention. A generated column is
preferred over a trigger because it cannot drift: there is no code path that updates `subject`
without updating the vector.

**Migration `0010_ticket_search`.** Adding a STORED generated column rewrites the table; note it in
the migration. At current volumes this is seconds, and saying so now prevents someone assuming it is
free at 10× the data.

---

## 3. Search Scope — the INV-9 Hazard

Search is the single most likely place to leak ticket existence, because relevance ranking naturally
wants to run before authorization.

**The rule: scope first, then rank.** The role scope that `TicketService.list_tickets` already builds
(`customer_id` for customers, `assignee_id` for agents, unrestricted for dispatcher/admin) is applied
as a `WHERE` clause in the *same* query as the FTS predicate. There is no code path that ranks
globally and filters afterwards, and no "total results" count computed outside the scope — a count
that includes invisible rows is itself the leak.

Search is exposed to **every** role, including customers searching their own requests. The scope
makes that safe, and it is the natural way for a customer with many requests to find one.

---

## 4. Filters

`GET /api/v1/tickets` gains query parameters, all optional and all combinable with the existing
`status`, `priority`, `breached`, `assigned`, `limit`, `cursor`:

| Param | Type | Notes |
|---|---|---|
| `q` | str (1..200) | Full-text query. Ranked; see §5 for its effect on ordering. |
| `category` | Category | |
| `tier` | CustomerTier | Joins `customer`. Dispatcher/admin only — see below. |
| `assignee_id` | UUID | Dispatcher/admin only. Agents already see only their own. |
| `customer_id` | UUID | Dispatcher/admin only. |
| `created_after` / `created_before` | datetime | Half-open `[after, before)`. |
| `escalated` | bool | `escalation_level > 0`. |

**Filters a role may not use are rejected 403, not silently ignored.** A customer passing
`customer_id` for someone else must not receive an empty list that they could interpret as "no
tickets" — it must be a clear refusal. Silently dropping the parameter is the subtler bug: it returns
*their own* tickets for a query that asked for someone else's, which reads as a successful answer to
the wrong question.

Repository work lands in `TicketRepository.list_scoped`, which already takes keyword filters and
composes them into one statement. The signature grows; the shape does not change.

---

## 5. Ordering & Pagination

Keyset pagination on `(created_at DESC, id DESC)` is preserved for every filtered list — the reasons
in `core/pagination.py` are unchanged.

**Search is the exception, and it needs its own cursor.** Relevance ordering breaks the keyset
because `ts_rank_cd` is not a stored, unique, monotonic key. Rather than fall back to OFFSET (which
`docs/API.md §4` rules out for good reason), when `q` is present:

- Ordering becomes `(rank DESC, created_at DESC, id DESC)`.
- The cursor encodes `(rank, created_at, id)` — the same opaque base64 round-trip, one extra
  component.
- `decode_cursor` accepts both the 2-part and 3-part forms; a cursor from a non-search query used on
  a search query (or vice versa) is rejected **400** rather than silently misinterpreted.

This keeps the "pages neither skip nor repeat" guarantee that motivated keyset in the first place.

---

## 6. Saved Views

A named filter combination, owned by one staff user.

```sql
CREATE TABLE saved_view (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id   uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  name       text NOT NULL,
  filters    jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_id, name)
);
```

**Migration `0011_saved_view`.**

- Staff only. A customer has two requests-list states; a saved view would be furniture.
- `ON DELETE CASCADE` is safe here and deliberate — unlike tickets and audit rows, a saved view has
  no historical value once its owner is gone. (Staff are deactivated, not deleted, so this is a
  belt-and-braces clause, not a routine path.)
- `filters` stores the query parameters as an object and is **validated against the same Pydantic
  model the list endpoint uses** on write. An unvalidated blob would let a saved view carry a filter
  its owner is not allowed to use — the role check in §4 must apply at save time and at execution
  time, because roles change.
- A saved view is private to its owner. Sharing is out of scope.

### Endpoints — `docs/API.md` new §13

| Method | Path | Authz |
|---|---|---|
| GET | `/api/v1/saved-views` | AGENT, DISPATCHER, ADMIN — own only |
| POST | `/api/v1/saved-views` | same; 422 on duplicate name or invalid filters |
| DELETE | `/api/v1/saved-views/{id}` | own only; another owner's id → **404** |

No update endpoint: renaming or re-filtering is delete + create, and the object is small enough that
a PATCH earns nothing.

---

## 7. Structure

New: `app/models/saved_view.py`, `app/repositories/saved_view_repo.py`,
`app/services/saved_view_service.py`, `app/schemas/saved_view.py`,
`app/api/v1/saved_views.py` (registered in `api/v1/router.py`).

Search itself adds **no** new service. It is a filter on ticket listing and belongs in
`TicketService.list_tickets`; giving it a `SearchService` would split one query's authorization
across two files, which is how scope bugs happen.

---

## 8. Tests

`tests/unit`
- 3-part cursor encode/decode round-trip; cross-form cursor rejected.
- Filter model validation, including role-forbidden combinations.

`tests/integration` (`@pytest.mark.db`)
- Ranking: subject match outranks body match for the same term (weighting works).
- Keyset stability under search: insert a new ticket mid-pagination, assert no skip or repeat.
- Generated column stays correct after a subject update.

`tests/api`
- **INV-9 under search (the critical test):** customer A searches a term that appears only in
  customer B's ticket → empty result, not 403, not a leaked count.
- Agent searching a term in an unassigned ticket → empty.
- Customer passing `assignee_id` / `tier` / another `customer_id` → **403**.
- Saved view CRUD; another user's view id → **404**; duplicate name → 422.
- Saving a view containing a filter the owner's role may not use → **403**.

---

## 9. Definition of Done

`0010`–`0011` apply and reverse; search results are scope-then-rank with an API test proving no
cross-customer leak; search pagination neither skips nor repeats; saved views validate filters
against role at both write and execution time.
