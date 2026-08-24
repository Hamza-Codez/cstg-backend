"""Ticket state machine (docs/SPEC/TICKET_LIFECYCLE.md §2).

Pure: legality and per-transition authorization are decided here from the
transition table, with no I/O. The service applies the decision and performs the
guarded UPDATE.
"""

from app.models.enums import Role, TicketStatus

_LEGAL_TRANSITIONS = {
    (TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
    (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
    (TicketStatus.RESOLVED, TicketStatus.CLOSED),
}

# The "Authorized roles" column of the transition table. AUTHORIZATION.md §3 is
# the authoritative matrix and agrees: DISPATCHER may close (T3) but may not
# start or resolve, and CUSTOMER may drive no transition at all.
#
# AGENT here means the *assigned* agent; that narrowing is object-level and is
# enforced in the service, which knows the ticket.
_AUTHORIZED_ROLES: dict[tuple[TicketStatus, TicketStatus], frozenset[Role]] = {
    (TicketStatus.OPEN, TicketStatus.IN_PROGRESS): frozenset({Role.AGENT, Role.ADMIN}),
    (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED): frozenset({Role.AGENT, Role.ADMIN}),
    (TicketStatus.RESOLVED, TicketStatus.CLOSED): frozenset(
        {Role.AGENT, Role.DISPATCHER, Role.ADMIN}
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
