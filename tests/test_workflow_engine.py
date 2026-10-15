import uuid

from app.workflows.dispatch import run_pending_workflows
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Customer,
    Tenant,
    Ticket,
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTrigger,
)

STATUS_COMPLETED_CONDITION = {
    "key": "n1",
    "type": "condition",
    "field": "status",
    "op": "eq",
    "value": "completed",
}


async def _make_tenant_and_customer(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, name="Jane", phone="+15550001111")
    db.add(customer)
    await db.flush()
    await db.commit()
    return tenant.id, customer.id


async def test_workflow_runs_action_when_condition_passes(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _make_tenant_and_customer(db_session)

    workflow = Workflow(
        tenant_id=tenant_id,
        name="Follow up on completed calls",
        trigger=WorkflowTrigger.CALL_ENDED,
        is_active=True,
        definition={
            "nodes": [
                STATUS_COMPLETED_CONDITION,
                {
                    "key": "n2",
                    "type": "action",
                    "action": "create_ticket",
                    "params": {
                        "subject": "Follow up on call",
                        "customer_id": "{{customer_id}}",
                    },
                },
            ]
        },
    )
    db_session.add(workflow)
    await db_session.commit()

    await run_pending_workflows(
        db_session,
        tenant_id=tenant_id,
        trigger=WorkflowTrigger.CALL_ENDED,
        payload={"status": "completed", "customer_id": str(customer_id)},
    )

    tickets = (
        await db_session.execute(select(Ticket).where(Ticket.tenant_id == tenant_id))
    ).scalars().all()
    assert len(tickets) == 1
    assert tickets[0].subject == "Follow up on call"

    runs = (
        await db_session.execute(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow.id))
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == WorkflowRunStatus.COMPLETED


async def test_workflow_stops_when_condition_fails(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _make_tenant_and_customer(db_session)

    workflow = Workflow(
        tenant_id=tenant_id,
        name="Follow up on completed calls",
        trigger=WorkflowTrigger.CALL_ENDED,
        is_active=True,
        definition={
            "nodes": [
                STATUS_COMPLETED_CONDITION,
                {
                    "key": "n2",
                    "type": "action",
                    "action": "create_ticket",
                    "params": {
                        "subject": "Should not be created",
                        "customer_id": "{{customer_id}}",
                    },
                },
            ]
        },
    )
    db_session.add(workflow)
    await db_session.commit()

    await run_pending_workflows(
        db_session,
        tenant_id=tenant_id,
        trigger=WorkflowTrigger.CALL_ENDED,
        payload={"status": "no_answer", "customer_id": str(customer_id)},
    )

    tickets = (
        await db_session.execute(select(Ticket).where(Ticket.tenant_id == tenant_id))
    ).scalars().all()
    assert tickets == []
