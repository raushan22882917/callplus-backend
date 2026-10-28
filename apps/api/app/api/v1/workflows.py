import uuid

from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.workflow import (
    RecipeCreate,
    WorkflowCreate,
    WorkflowRead,
    WorkflowRunRead,
    WorkflowUpdate,
)
from app.workflows import recipes
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Workflow, WorkflowRun

router = APIRouter(tags=["workflows"])


class WorkflowRepository(TenantScopedRepository[Workflow]):
    model = Workflow


@router.get("/workflows", response_model=list[WorkflowRead])
async def list_workflows(
    ctx: RequestContext = Depends(require_permission(Permissions.WORKFLOWS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[Workflow]:
    return await WorkflowRepository(db, ctx.tenant_id).list()


@router.post("/workflows", response_model=WorkflowRead, status_code=201)
async def create_workflow(
    payload: WorkflowCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.WORKFLOWS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Workflow:
    workflow = await WorkflowRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await db.commit()
    await db.refresh(workflow)
    return workflow


@router.get("/workflows/recipes")
async def list_recipes(
    ctx: RequestContext = Depends(require_permission(Permissions.WORKFLOWS_READ)),
) -> list[dict]:
    """Ready-made automations, in plain words, for people who do not want to build one."""
    return recipes.catalogue()


@router.post("/workflows/from-recipe", response_model=WorkflowRead, status_code=201)
async def create_from_recipe(
    payload: RecipeCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.WORKFLOWS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Workflow:
    """Switches on one ready-made automation, with the owner's answers to its questions."""
    try:
        recipe = recipes.get_recipe(payload.recipe)
        definition = recipes.build(recipe, payload.answers)
    except recipes.RecipeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    repo = WorkflowRepository(db, ctx.tenant_id)
    if any((w.definition or {}).get("recipe") == recipe.id for w in await repo.list()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have this automation. Turn it on or off from the list.",
        )
    workflow = await repo.create(
        name=(payload.name or recipe.title).strip()[:255],
        trigger=recipe.trigger,
        is_active=True,
        definition=definition,
    )
    await db.commit()
    await db.refresh(workflow)
    return workflow


@router.get("/workflows/{workflow_id}", response_model=WorkflowRead)
async def get_workflow(
    workflow_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.WORKFLOWS_READ)),
    db: AsyncSession = Depends(get_db),
) -> Workflow:
    return await WorkflowRepository(db, ctx.tenant_id).get(workflow_id)


@router.patch("/workflows/{workflow_id}", response_model=WorkflowRead)
async def update_workflow(
    workflow_id: uuid.UUID,
    payload: WorkflowUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.WORKFLOWS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Workflow:
    repo = WorkflowRepository(db, ctx.tenant_id)
    workflow = await repo.get(workflow_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(workflow, field, value)
    await db.commit()
    await db.refresh(workflow)
    return workflow


@router.get("/workflows/{workflow_id}/runs", response_model=list[WorkflowRunRead])
async def list_workflow_runs(
    workflow_id: uuid.UUID,
    limit: int = Query(default=50, le=200),
    ctx: RequestContext = Depends(require_permission(Permissions.WORKFLOWS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[WorkflowRun]:
    await WorkflowRepository(db, ctx.tenant_id).get(workflow_id)  # 404s if not in this tenant
    result = await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.tenant_id == ctx.tenant_id, WorkflowRun.workflow_id == workflow_id)
        .order_by(WorkflowRun.started_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
