"""Ticket state machine (docs/SPEC/TICKET_LIFECYCLE.md §2, spec05 §4).

Pure: legality and per-transition authorization are decided here from the
transition table, with no I/O. The service applies the decision and performs the
guarded UPDATE.

v2 grows the table from three rows to six. T4/T5 pause and resume the SLA clock;
T6 reopens a resolved ticket.
"""

from app.models.enums import Role, TicketStatus

_LEGAL_TRANSITIONS = {
    (TicketStatus.OPEN, TicketStatus.IN_PROGRESS),  # T1 start
    (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),  # T2 resolve
    (TicketStatus.RESOLVED, TicketStatus.CLOSED),  # T3 close
    (TicketStatus.IN_PROGRESS, TicketStatus.PENDING_CUSTOMER),  # T4 await customer
    (TicketStatus.PENDING_CUSTOMER, TicketStatus.IN_PROGRESS),  # T5 resume
    (TicketStatus.RESOLVED, TicketStatus.IN_PROGRESS),  # T6 reopen
}

# The "Authorized roles" column of the transition table. AUTHORIZATION.md §3 is
# the authoritative matrix and agrees.
#
# AGENT here means the *assigned* agent; that narrowing is object-level and is
# enforced in the service, which knows the ticket. CUSTOMER likewise means the
# *owning* customer.
#
# T5 and T6 admit CUSTOMER, which breaks the v1 rule that a customer drives no
# transition. Both are the customer answering a question the system asked them —
# "we're waiting on you" and "we think this is fixed" — and nobody else can
# answer either. The transitions that assert work was *done* (T1, T2, T3) remain
# staff-only, which is what the original rule was protecting.
_AUTHORIZED_ROLES: dict[tuple[TicketStatus, TicketStatus], frozenset[Role]] = {
    (TicketStatus.OPEN, TicketStatus.IN_PROGRESS): frozenset({Role.AGENT, Role.ADMIN}),
    (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED): frozenset({Role.AGENT, Role.ADMIN}),
    (TicketStatus.RESOLVED, TicketStatus.CLOSED): frozenset(
        {Role.AGENT, Role.DISPATCHER, Role.ADMIN}
    ),
    (TicketStatus.IN_PROGRESS, TicketStatus.PENDING_CUSTOMER): frozenset({Role.AGENT, Role.ADMIN}),
    (TicketStatus.PENDING_CUSTOMER, TicketStatus.IN_PROGRESS): frozenset(
        {Role.AGENT, Role.ADMIN, Role.CUSTOMER}
    ),
    (TicketStatus.RESOLVED, TicketStatus.IN_PROGRESS): frozenset(
        {Role.AGENT, Role.DISPATCHER, Role.ADMIN, Role.CUSTOMER}
    ),
}


def is_legal_transition(from_status: TicketStatus, to_status: TicketStatus) -> bool:
    """Whether this (from -> to) pair appears in the transition table (INV-3)."""
    return (from_status, to_status) in _LEGAL_TRANSITIONS


def roles_for_transition(from_status: TicketStatus, to_status: TicketStatus) -> frozenset[Role]:
    """Roles permitted to drive this transition; empty if the pair is illegal."""
    return _AUTHORIZED_ROLES.get((from_status, to_status), frozenset())


def is_role_authorized(role: Role, from_status: TicketStatus, to_status: TicketStatus) -> bool:
    """Coarse role check for a transition (INV-4)."""
    return role in roles_for_transition(from_status, to_status)


def is_reopen(from_status: TicketStatus, to_status: TicketStatus) -> bool:
    """T6. Named because three call sites care and a tuple comparison does not read."""
    return from_status is TicketStatus.RESOLVED and to_status is TicketStatus.IN_PROGRESS


def pauses_clock(to_status: TicketStatus) -> bool:
    """T4 — the clock stops on arrival at PENDING_CUSTOMER."""
    return to_status is TicketStatus.PENDING_CUSTOMER


def resumes_clock(from_status: TicketStatus) -> bool:
    """T5 — the clock restarts on departure from PENDING_CUSTOMER."""
    return from_status is TicketStatus.PENDING_CUSTOMER
