"""Metrics v2 over HTTP: time series, per-agent, and export (spec09 §8).

The `DISPATCHER` denial gets its own case here rather than riding along in the
authorization matrix: dispatchers hold every *other* wide capability, so their
exclusion from metrics is the one an ordinary "make it work for the dispatcher"
change would quietly undo.
"""

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.models.ticket import Ticket
from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    create_dispatcher,
    get_auth_token,
    seed_priority_rules,
)

TIMESERIES = "/api/v1/metrics/timeseries"
AGENTS = "/api/v1/metrics/agents"
EXPORT = "/api/v1/metrics/export/tickets.csv"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _open_ticket(client: AsyncClient, token: str, subject: str = "S") -> str:
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": subject, "body": "B", "category": "GENERAL"},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


# ── Authorization ──────────────────────────────────────────────────────────────


@pytest.mark.db
@pytest.mark.parametrize("path", [TIMESERIES, AGENTS, EXPORT])
async def test_every_non_admin_role_is_refused(
    client: AsyncClient, db_session: AsyncSession, path: str
) -> None:
    """`DISPATCHER` included. Per-agent performance and a full row dump are
    management information, not dispatch tooling (docs/AUTHORIZATION.md §3)."""
    suffix = path.rsplit("/", 1)[-1].replace(".", "")
    customer = await create_customer(db_session, f"mv2c_{suffix}@example.com")
    agent = await create_agent(db_session, f"mv2a_{suffix}@example.com")
    dispatcher = await create_dispatcher(db_session, f"mv2d_{suffix}@example.com")
    await seed_priority_rules(db_session)

    for principal in (customer, agent, dispatcher):
        token = await get_auth_token(client, principal.email)
        resp = await client.get(path, headers=auth(token))
        assert resp.status_code == 403, f"{principal.email} reached {path}: {resp.text}"


@pytest.mark.db
@pytest.mark.parametrize("path", [TIMESERIES, AGENTS, EXPORT])
async def test_anonymous_is_401_not_403(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401


# ── Time series ────────────────────────────────────────────────────────────────


@pytest.mark.db
async def test_quiet_days_are_zero_valued_points_not_gaps(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """**The empty-bucket rule.**

    A chart handed a gap draws a straight line across it and misreports a dead
    week as steady activity. Every bucket in range must be present.
    """
    customer = await create_customer(db_session, "ts_c@example.com")
    admin = await create_admin(db_session, "ts_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    await _open_ticket(client, t_cust, "Today only")

    end = now() + timedelta(days=1)
    start = end - timedelta(days=10)
    resp = await client.get(
        TIMESERIES,
        params={
            "metric": "created",
            "bucket": "day",
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
        headers=auth(t_admin),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["points"]) >= 10, "silent days must still be points"
    assert sum(p["value"] for p in data["points"]) == 1
    assert [p for p in data["points"] if p["value"] == 0], "zeros, not omissions"
    assert data["bucket"] == "day"
    assert data["metric"] == "created"


@pytest.mark.db
async def test_a_range_over_the_bucket_cap_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """422, not a truncated series. Silently narrowing the range would answer a
    question the caller did not ask."""
    admin = await create_admin(db_session, "ts_cap@example.com")
    t_admin = await get_auth_token(client, admin.email)

    end = datetime(2026, 1, 1, tzinfo=UTC)
    start = end - timedelta(days=400)
    resp = await client.get(
        TIMESERIES,
        params={"bucket": "day", "from": start.isoformat(), "to": end.isoformat()},
        headers=auth(t_admin),
    )
    assert resp.status_code == 422, resp.text
    assert "366" in resp.json()["error"]["message"]

    # The same span is fine at a coarser bucket — the cap is on points, not days.
    coarse = await client.get(
        TIMESERIES,
        params={"bucket": "week", "from": start.isoformat(), "to": end.isoformat()},
        headers=auth(t_admin),
    )
    assert coarse.status_code == 200


@pytest.mark.db
async def test_an_inverted_range_is_refused(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await create_admin(db_session, "ts_inv@example.com")
    t_admin = await get_auth_token(client, admin.email)
    at = datetime(2026, 1, 1, tzinfo=UTC)

    resp = await client.get(
        TIMESERIES,
        params={"from": at.isoformat(), "to": (at - timedelta(days=1)).isoformat()},
        headers=auth(t_admin),
    )
    assert resp.status_code == 422


@pytest.mark.db
async def test_an_unknown_metric_is_rejected_by_the_schema(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The Literal is the guard. Without it an unmapped name would reach the
    repository's metric->column dict and raise KeyError as a 500.

    400, not 422: a malformed request is a `ValidationError`, while 422 is
    reserved for a well-formed request that breaks a business rule — which is
    what the range cap above is (docs/API.md error taxonomy).
    """
    admin = await create_admin(db_session, "ts_bad@example.com")
    t_admin = await get_auth_token(client, admin.email)

    resp = await client.get(TIMESERIES, params={"metric": "profit"}, headers=auth(t_admin))
    assert resp.status_code == 400
    assert (
        await client.get(TIMESERIES, params={"bucket": "fortnight"}, headers=auth(t_admin))
    ).status_code == 400


# ── Per-agent ──────────────────────────────────────────────────────────────────


@pytest.mark.db
async def test_agent_metrics_ship_the_attribution_caveat_in_the_payload(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The caveat is part of the contract, not documentation.

    An unlabelled approximation in a performance metric is worse than no
    metric, because someone will manage against it.
    """
    customer = await create_customer(db_session, "ag_c@example.com")
    agent = await create_agent(db_session, "ag_a@example.com")
    admin = await create_admin(db_session, "ag_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )

    resp = await client.get(AGENTS, headers=auth(t_admin))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "current owner" in data["attribution_note"]
    row = next(i for i in data["items"] if i["agent"]["id"] == str(agent.id))
    assert row["open_tickets"] == 1
    assert row["resolved_in_period"] == 0
    assert row["sla_met_rate"] == 0.0
    # No ceiling configured, so load is unknown rather than zero.
    assert row["current_load_pct"] is None


@pytest.mark.db
async def test_an_inactive_agent_holding_tickets_is_still_reported(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Excluding them would make the period's totals stop reconciling with the
    overview, and a departed agent's history is what a period report is for."""
    customer = await create_customer(db_session, "ina_c@example.com")
    agent = await create_agent(db_session, "ina_a@example.com")
    admin = await create_admin(db_session, "ina_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )

    deactivated = await client.patch(
        f"/api/v1/users/{agent.id}",
        json={"is_active": False},
        headers=auth(t_admin),
    )
    assert deactivated.status_code == 200, deactivated.text

    data = (await client.get(AGENTS, headers=auth(t_admin))).json()
    row = next(i for i in data["items"] if i["agent"]["id"] == str(agent.id))
    assert row["agent"]["is_active"] is False
    assert row["open_tickets"] == 1


@pytest.mark.db
async def test_load_is_a_fraction_of_the_agents_own_ceiling(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "cap_c@example.com")
    agent = await create_agent(db_session, "cap_a@example.com")
    admin = await create_admin(db_session, "cap_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    capped = await client.patch(
        f"/api/v1/users/{agent.id}",
        json={"max_open_tickets": 4},
        headers=auth(t_admin),
    )
    assert capped.status_code == 200, capped.text

    for n in range(2):
        tid = await _open_ticket(client, t_cust, f"L{n}")
        await client.post(
            f"/api/v1/tickets/{tid}/assignment",
            json={"assignee_id": str(agent.id)},
            headers=auth(t_admin),
        )

    data = (await client.get(AGENTS, headers=auth(t_admin))).json()
    row = next(i for i in data["items"] if i["agent"]["id"] == str(agent.id))
    assert row["current_load_pct"] == 0.5


# ── Export ─────────────────────────────────────────────────────────────────────


def _rows(body: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(body)))


@pytest.mark.db
async def test_export_is_an_attachment_and_carries_no_ticket_body(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Operational data, not a content dump — bodies carry whatever a customer
    pasted into them."""
    customer = await create_customer(db_session, "ex_c@example.com")
    admin = await create_admin(db_session, "ex_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    await client.post(
        "/api/v1/tickets",
        json={
            "subject": "Printer offline",
            "body": "My card number is 4111 1111 1111 1111",
            "category": "GENERAL",
        },
        headers=auth(t_cust),
    )

    resp = await client.get(EXPORT, headers=auth(t_admin))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert ".csv" in resp.headers["content-disposition"]

    body = resp.text
    assert "4111 1111 1111 1111" not in body
    assert "body" not in body.splitlines()[0].split(",")

    rows = _rows(body)
    assert len(rows) == 1
    assert rows[0]["subject"] == "Printer offline"
    assert rows[0]["customer_name"] == "Test Cust"
    assert rows[0]["assignee_name"] == "", "unassigned exports blank, not 'None'"


@pytest.mark.db
async def test_a_formula_subject_is_neutralised_in_the_file(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """End-to-end, not just at the domain helper: the escaping must survive the
    repository row and `csv.writer`."""
    customer = await create_customer(db_session, "inj_c@example.com")
    admin = await create_admin(db_session, "inj_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    await client.post(
        "/api/v1/tickets",
        json={
            "subject": '=HYPERLINK("http://evil.test","Click")',
            "body": "B",
            "category": "GENERAL",
        },
        headers=auth(t_cust),
    )

    rows = _rows((await client.get(EXPORT, headers=auth(t_admin))).text)
    assert rows[0]["subject"].startswith("'="), rows[0]["subject"]


@pytest.mark.db
async def test_export_returns_exactly_what_the_list_endpoint_would(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Both build their WHERE clause from `filter_predicates`. This is the test
    that fails if someone re-implements a filter in one of the two."""
    customer = await create_customer(db_session, "flt_c@example.com")
    admin = await create_admin(db_session, "flt_adm@example.com")
    agent = await create_agent(db_session, "flt_a@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    kept = await _open_ticket(client, t_cust, "Keep me")
    await _open_ticket(client, t_cust, "Leave me")

    await client.post(
        f"/api/v1/tickets/{kept}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )

    params = {"assigned": "true"}
    listed = await client.get("/api/v1/tickets", params=params, headers=auth(t_admin))
    exported = await client.get(EXPORT, params=params, headers=auth(t_admin))

    assert {i["id"] for i in listed.json()["items"]} == {
        r["ticket_id"] for r in _rows(exported.text)
    }


@pytest.mark.db
async def test_over_the_row_cap_the_export_is_refused_before_any_bytes(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**422, not a short file.** Once a streaming response has begun there is
    no way to turn it back into an error, so the cap is checked up front."""
    from app.config import get_settings

    customer = await create_customer(db_session, "cap2_c@example.com")
    admin = await create_admin(db_session, "cap2_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    for n in range(3):
        await _open_ticket(client, t_cust, f"C{n}")

    monkeypatch.setattr(get_settings(), "export_max_rows", 2)

    resp = await client.get(EXPORT, headers=auth(t_admin))
    assert resp.status_code == 422, resp.text
    assert "narrow" in resp.json()["error"]["message"].lower()
    assert "ticket_id" not in resp.text, "no partial file may have been emitted"


@pytest.mark.db
async def test_export_reports_the_pause_columns(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`sla_due_at` and `sla_paused_seconds` are both present, so a reader can
    reconstruct why a ticket did or did not breach."""
    customer = await create_customer(db_session, "pz_c@example.com")
    admin = await create_admin(db_session, "pz_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await db_session.execute(update(Ticket).where(Ticket.id == tid).values(sla_paused_seconds=3600))
    await db_session.commit()

    row = _rows((await client.get(EXPORT, headers=auth(t_admin))).text)[0]
    assert row["sla_paused_seconds"] == "3600"
    assert row["sla_due_at"]
    assert row["reopen_count"] == "0"
    assert row["escalation_level"] == "0"
