"""Workflow interpreter.

A workflow's `definition` is a JSON object: `{"nodes": [...]}`, an ORDERED
list executed top to bottom — not a general branching DAG. That's a
deliberate scope decision: every example in the product design ("call
ended -> is a lead? -> score > 70? -> create task -> send WhatsApp") is a
straight-line pipeline, so a linear list is the simplest thing that's
actually correct; a full graph executor would be unused complexity today.

Two node types:
  {"key": "...", "type": "condition", "field": "...",
   "op": "eq"|"neq"|"in"|"contains"|"exists", "value": ...}
    Evaluated against the trigger payload. If false, the run stops here
    (COMPLETED, not FAILED — "condition not met" is a normal outcome).
  {"key": "...", "type": "action", "action": "<tool registry name>", "params": {...}}
    Runs a tools/handlers.py handler (the same ones AI agents call) with
    `params` values resolved against the payload — a param value of exactly
    "{{field}}" is replaced with payload["field"]; anything else passes
    through as a literal.
"""

import uuid
from typing import Any

from app.tools.context import ToolContext
from app.tools.registry import execute_tool
from sqlalchemy.ext.asyncio import AsyncSession


class WorkflowExecutionError(Exception):
    pass


def _resolve(value: Any, payload: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
        return payload.get(value[2:-2].strip())
    return value


def _evaluate_condition(node: dict, payload: dict[str, Any]) -> bool:
    field, op, expected = node.get("field"), node.get("op"), node.get("value")
    actual = payload.get(field)
    if op == "exists":
        return actual is not None
    if op == "eq":
        return actual == expected
    if op == "neq":
        return actual != expected
    if op == "in":
        return actual in (expected or [])
    if op == "contains":
        return expected in (actual or "")
    raise WorkflowExecutionError(f"unknown condition operator: {op}")


async def run_definition(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    definition: dict[str, Any],
    payload: dict[str, Any],
    on_node_complete,
) -> None:
    """Runs each node in order, calling `on_node_complete(node_key, status,
    result)` after each — the caller (dispatch.py) uses this to persist
    WorkflowTask rows without this function needing to know about them."""

    tool_ctx = ToolContext(
        db=db,
        tenant_id=tenant_id,
        agent_id=None,
        call_id=_as_uuid(payload.get("call_id")),
        conversation_id=None,
        customer_id=_as_uuid(payload.get("customer_id")),
    )

    for node in definition.get("nodes", []):
        node_key = node["key"]
        if node["type"] == "condition":
            passed = _evaluate_condition(node, payload)
            await on_node_complete(node_key, "completed", {"passed": passed})
            if not passed:
                return
        elif node["type"] == "action":
            params = {k: _resolve(v, payload) for k, v in node.get("params", {}).items()}
            result = await execute_tool(node["action"], tool_ctx, params)
            status = "failed" if "error" in result else "completed"
            await on_node_complete(node_key, status, result)
            if status == "failed":
                raise WorkflowExecutionError(f"action {node['action']} failed: {result['error']}")
        else:
            raise WorkflowExecutionError(f"unknown node type: {node['type']}")


def _as_uuid(value: Any) -> uuid.UUID | None:
    return uuid.UUID(value) if isinstance(value, str) else None
