"""MCP server: auth (dedicated + developer keys), scopes, tenant isolation."""

import uuid
from types import SimpleNamespace

import pytest
from app.core.api_keys import generate_api_key, generate_developer_key
from app.mcp import extended, server
from mcp.server.mcpserver.exceptions import ToolError

from packages.database.models import ApiKey, Call, McpApiKey, Tenant
from packages.database.session import session_scope


def _ctx(token: str | None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(headers=headers)


async def _tenant(name: str) -> uuid.UUID:
    async with session_scope() as db:
        tenant = Tenant(name=name, slug=name.lower())
        db.add(tenant)
        await db.commit()
        return tenant.id


async def _mcp_key(tenant_id: uuid.UUID) -> str:
    plaintext, prefix, hashed = generate_api_key()
    async with session_scope() as db:
        db.add(McpApiKey(tenant_id=tenant_id, name="t", key_prefix=prefix, hashed_key=hashed))
        await db.commit()
    return plaintext


async def _dev_key(tenant_id: uuid.UUID, scopes: list[str]) -> str:
    plaintext, prefix, hashed = generate_developer_key("test")
    async with session_scope() as db:
        db.add(
            ApiKey(
                tenant_id=tenant_id,
                name="d",
                key_prefix=prefix,
                hashed_key=hashed,
                mode="test",
                scopes=scopes,
            )
        )
        await db.commit()
    return plaintext


async def test_catalogue_and_annotations():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert {"trigger_call", "list_calls", "get_call_transcript", "book_appointment"} <= set(tools)
    assert tools["list_calls"].annotations.read_only_hint is True
    assert tools["trigger_call"].annotations.open_world_hint is True
    assert {p.name for p in await server.mcp.list_prompts()} >= {"customer_360"}


async def test_missing_and_bad_key_rejected():
    with pytest.raises(ToolError):
        await extended.list_calls(_ctx(None))
    with pytest.raises(ToolError):
        await extended.list_calls(_ctx("ccb_mcp_not-a-real-key"))


async def test_mcp_key_reads_only_its_own_tenant():
    a, b = await _tenant("alpha"), await _tenant("beta")
    async with session_scope() as db:
        call = Call(tenant_id=a, direction="inbound", status="completed", from_number="+1555")
        db.add(call)
        await db.commit()
        call_id = str(call.id)
    key_a, key_b = await _mcp_key(a), await _mcp_key(b)

    listed = await extended.list_calls(_ctx(key_a))
    assert [c["call_id"] for c in listed["calls"]] == [call_id]
    assert (await extended.list_calls(_ctx(key_b)))["calls"] == []
    with pytest.raises(ToolError, match="not found"):
        await extended.get_call(_ctx(key_b), call_id)


async def test_developer_key_scopes_are_enforced():
    t = await _tenant("gamma")
    read_only = await _dev_key(t, ["calls:read"])
    assert (await extended.list_calls(_ctx(read_only)))["calls"] == []
    with pytest.raises(ToolError, match="customers:write"):
        await extended.create_customer(_ctx(read_only), name="X", phone="+15551234567")

    writer = await _dev_key(t, ["customers:write"])
    created = await extended.create_customer(_ctx(writer), name="X", phone="+15551234567")
    assert created["created"] is True
    again = await extended.create_customer(_ctx(writer), name="X", phone="15551234567")
    assert again == {"customer_id": created["customer_id"], "created": False}
    # a write scope implies read of the same family
    found = await extended.search_customers(_ctx(writer), "X")
    assert len(found["customers"]) == 1


async def test_revoked_key_rejected():
    from datetime import UTC, datetime

    t = await _tenant("delta")
    key = await _mcp_key(t)
    async with session_scope() as db:
        row = (await db.execute(McpApiKey.__table__.select())).first()
        obj = await db.get(McpApiKey, row.id)
        obj.revoked_at = datetime.now(UTC)
        await db.commit()
    with pytest.raises(ToolError, match="revoked"):
        await extended.list_ai_agents(_ctx(key))


async def test_input_validation():
    t = await _tenant("eps")
    key = await _mcp_key(t)
    with pytest.raises(ToolError, match="UUID"):
        await extended.get_call(_ctx(key), "nope")
    with pytest.raises(ToolError, match="one of"):
        await extended.list_calls(_ctx(key), status="bogus")
    stats = await extended.get_call_stats(_ctx(key), days=999)
    assert stats["total_calls"] == 0


async def test_csat_summary_tool_is_scoped_and_tenant_bound():
    t = await _tenant("zeta")
    reader = await _dev_key(t, ["calls:read"])
    assert (await extended.get_csat_summary(_ctx(reader)))["responses"] == 0
    no_scope = await _dev_key(t, ["agents:read"])
    with pytest.raises(ToolError, match="calls:read"):
        await extended.get_csat_summary(_ctx(no_scope))
