import asyncio
import os
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.core import clock
from app.core.security import get_password_hash
from app.database import create_engine, create_session_factory
from app.models.comment import Comment
from app.models.customer import Customer
from app.models.enums import (
    ActorType,
    Category,
    CommentType,
    CustomerTier,
    EventType,
    Priority,
    Role,
    TicketStatus,
)
from app.models.priority_rule import PriorityRule
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import AppUser

PRIORITY_MATRIX = {
    (CustomerTier.ENTERPRISE, Category.OUTAGE): Priority.CRITICAL,
    (CustomerTier.ENTERPRISE, Category.BILLING): Priority.HIGH,
    (CustomerTier.ENTERPRISE, Category.TECHNICAL): Priority.HIGH,
    (CustomerTier.ENTERPRISE, Category.GENERAL): Priority.MEDIUM,
    (CustomerTier.BUSINESS, Category.OUTAGE): Priority.HIGH,
    (CustomerTier.BUSINESS, Category.BILLING): Priority.MEDIUM,
    (CustomerTier.BUSINESS, Category.TECHNICAL): Priority.MEDIUM,
    (CustomerTier.BUSINESS, Category.GENERAL): Priority.LOW,
    (CustomerTier.FREE, Category.OUTAGE): Priority.MEDIUM,
    (CustomerTier.FREE, Category.BILLING): Priority.LOW,
    (CustomerTier.FREE, Category.TECHNICAL): Priority.LOW,
    (CustomerTier.FREE, Category.GENERAL): Priority.LOW,
}


def validate_matrix() -> None:
    """Validate that the matrix is total across tier x category."""
    for tier in CustomerTier:
        for cat in Category:
            if (tier, cat) not in PRIORITY_MATRIX:
                raise ValueError(f"Missing priority mapping for {tier} x {cat}")


async def seed_db() -> None:
    validate_matrix()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        # Seed PriorityRule
        for (tier, category), priority in PRIORITY_MATRIX.items():
            stmt = insert(PriorityRule).values(tier=tier, category=category, priority=priority)
            stmt = stmt.on_conflict_do_update(
                index_elements=["tier", "category"], set_={"priority": stmt.excluded.priority}
            )
            await session.execute(stmt)

        # Seed Bootstrap Admin
        # Password comes from the environment so a real deployment never ships a
        # known credential; the default exists only for local development.
        admin_email = os.environ.get("APP_BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
        admin_password = os.environ.get("APP_BOOTSTRAP_ADMIN_PASSWORD", "admin-dev-password")
        result = await session.execute(select(AppUser).where(AppUser.email == admin_email))
        admin = result.scalar_one_or_none()

        if admin is not None:
            # Re-hash on every run so a rotated env password takes effect, and so
            # any pre-P2 placeholder hash is repaired rather than left unusable.
            admin.password_hash = get_password_hash(admin_password)
        else:
            admin = AppUser(
                email=admin_email,
                password_hash=get_password_hash(admin_password),
                name="Bootstrap Admin",
                role=Role.ADMIN,
            )
            session.add(admin)

        # Optional demo data. Off by default so a real deployment never gets a
        # known customer login; P12 builds this out for the demo environment.
        if os.environ.get("APP_SEED_DEMO_DATA", "").lower() in {"1", "true", "yes"}:
            # Demo staff so the agent and dispatcher workspaces are reachable.
            demo_staff = [
                ("agent@example.com", "Demo Agent", Role.AGENT),
                ("dispatcher@example.com", "Demo Dispatcher", Role.DISPATCHER),
            ]
            staff_password = os.environ.get("APP_DEMO_STAFF_PASSWORD", "staff-dev-password")
            for staff_email, staff_name, staff_role in demo_staff:
                found = await session.execute(select(AppUser).where(AppUser.email == staff_email))
                member = found.scalar_one_or_none()
                if member is None:
                    session.add(
                        AppUser(
                            email=staff_email,
                            password_hash=get_password_hash(staff_password),
                            name=staff_name,
                            role=staff_role,
                        )
                    )
                else:
                    member.password_hash = get_password_hash(staff_password)

            demo_email = "customer@example.com"
            existing = await session.execute(select(Customer).where(Customer.email == demo_email))
            demo_customer = existing.scalar_one_or_none()
            demo_password = os.environ.get("APP_DEMO_CUSTOMER_PASSWORD", "customer-dev-password")
            if demo_customer is None:
                session.add(
                    Customer(
                        email=demo_email,
                        password_hash=get_password_hash(demo_password),
                        name="Demo Customer",
                        tier=CustomerTier.BUSINESS,
                    )
                )
            else:
                demo_customer.password_hash = get_password_hash(demo_password)

            await session.flush()

            # Fetch agent and customer for seeding tickets
            agent_res = await session.execute(
                select(AppUser).where(AppUser.email == "agent@example.com")
            )
            agent = agent_res.scalar_one()

            customer_res = await session.execute(
                select(Customer).where(Customer.email == "customer@example.com")
            )
            customer = customer_res.scalar_one()

            # Seed tickets if there are none
            ticket_count = await session.execute(
                select(Ticket).where(Ticket.customer_id == customer.id)
            )
            if ticket_count.first() is None:
                # 1. Open Ticket (Unassigned)
                t1 = Ticket(
                    customer_id=customer.id,
                    subject="Login issue on mobile app",
                    body="I can't seem to log into the mobile app, it keeps spinning.",
                    category=Category.TECHNICAL,
                    priority=Priority.MEDIUM,
                    status=TicketStatus.OPEN,
                    deadline=clock.now() + timedelta(days=2),
                )
                session.add(t1)
                await session.flush()
                session.add(
                    TicketEvent(
                        ticket_id=t1.id,
                        type=EventType.CREATED,
                        actor_type=ActorType.CUSTOMER,
                        actor_id=customer.id,
                    )
                )

                # 2. In Progress Ticket (Assigned to Agent with comments)
                t2 = Ticket(
                    customer_id=customer.id,
                    assignee_id=agent.id,
                    subject="Billing discrepancy for last month",
                    body="I was charged twice for the Enterprise plan last month.",
                    category=Category.BILLING,
                    priority=Priority.HIGH,
                    status=TicketStatus.IN_PROGRESS,
                    deadline=clock.now() + timedelta(hours=4),
                )
                session.add(t2)
                await session.flush()
                session.add(
                    TicketEvent(
                        ticket_id=t2.id,
                        type=EventType.CREATED,
                        actor_type=ActorType.CUSTOMER,
                        actor_id=customer.id,
                    )
                )
                session.add(
                    TicketEvent(
                        ticket_id=t2.id,
                        type=EventType.ASSIGNMENT,
                        actor_type=ActorType.USER,
                        actor_id=agent.id,
                    )
                )
                session.add(
                    TicketEvent(
                        ticket_id=t2.id,
                        type=EventType.STATUS_CHANGE,
                        actor_type=ActorType.USER,
                        actor_id=agent.id,
                        from_status=TicketStatus.OPEN,
                        to_status=TicketStatus.IN_PROGRESS,
                    )
                )

                c1 = Comment(
                    ticket_id=t2.id,
                    author_id=agent.id,
                    type=CommentType.PUBLIC_REPLY,
                    body=(
                        "We are looking into this right now. "
                        "Could you confirm the date of the second charge?"
                    ),
                )
                session.add(c1)
                session.add(
                    TicketEvent(
                        ticket_id=t2.id,
                        type=EventType.COMMENT,
                        actor_type=ActorType.USER,
                        actor_id=agent.id,
                    )
                )

                # 3. Resolved Ticket
                t3 = Ticket(
                    customer_id=customer.id,
                    assignee_id=agent.id,
                    subject="How to reset password?",
                    body="I forgot my password for the reporting dashboard.",
                    category=Category.GENERAL,
                    priority=Priority.LOW,
                    status=TicketStatus.RESOLVED,
                    deadline=clock.now() - timedelta(days=1),
                    resolved_at=clock.now() - timedelta(days=1, hours=2),
                )
                session.add(t3)
                await session.flush()
                session.add(
                    TicketEvent(
                        ticket_id=t3.id,
                        type=EventType.CREATED,
                        actor_type=ActorType.CUSTOMER,
                        actor_id=customer.id,
                    )
                )
                session.add(
                    TicketEvent(
                        ticket_id=t3.id,
                        type=EventType.STATUS_CHANGE,
                        actor_type=ActorType.USER,
                        actor_id=agent.id,
                        from_status=TicketStatus.IN_PROGRESS,
                        to_status=TicketStatus.RESOLVED,
                    )
                )

        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_db())
    print("Seeding complete.")
