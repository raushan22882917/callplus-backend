from httpx import AsyncClient

from tests.test_call_control import _auth, _signup


async def test_custom_role_lifecycle_and_member_permissions(client: AsyncClient) -> None:
    owner = await _signup(client, slug="roles-co", email="owner@rolesco.com")

    perms = await client.get("/api/v1/permissions", headers=_auth(owner))
    assert "tickets.read" in {p["code"] for p in perms.json()}

    created = await client.post(
        "/api/v1/roles",
        headers=_auth(owner),
        json={"name": "Tier1 Support", "permission_codes": ["tickets.read", "customers.read"]},
    )
    assert created.status_code == 201, created.text
    role = created.json()
    assert role["permission_codes"] == ["customers.read", "tickets.read"]

    invited = await client.post(
        "/api/v1/users/invite",
        headers=_auth(owner),
        json={"email": "t1@rolesco.com", "full_name": "T1", "role_name": "Tier1 Support"},
    )
    assert invited.status_code == 201, invited.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "t1@rolesco.com", "password": invited.json()["temporary_password"]},
    )
    member = login.json()["tokens"]

    assert (await client.get("/api/v1/tickets", headers=_auth(member))).status_code == 200
    assert (await client.get("/api/v1/calls", headers=_auth(member))).status_code == 403

    # Editing the role takes effect on the member's very next request.
    patched = await client.patch(
        f"/api/v1/roles/{role['id']}",
        headers=_auth(owner),
        json={"permission_codes": ["calls.read"]},
    )
    assert patched.status_code == 200
    assert (await client.get("/api/v1/calls", headers=_auth(member))).status_code == 200
    assert (await client.get("/api/v1/tickets", headers=_auth(member))).status_code == 403

    # In use -> can't delete.
    assert (
        await client.delete(f"/api/v1/roles/{role['id']}", headers=_auth(owner))
    ).status_code == 409


async def test_role_guards(client: AsyncClient) -> None:
    owner = await _signup(client, slug="roleguard-co", email="owner@roleguardco.com")
    roles = (await client.get("/api/v1/roles", headers=_auth(owner))).json()
    system_role = next(r for r in roles if r["is_system"])

    # System roles are read-only, and their names are reserved.
    assert (
        await client.patch(
            f"/api/v1/roles/{system_role['id']}", headers=_auth(owner), json={"name": "Nope"}
        )
    ).status_code == 403
    dup = await client.post(
        "/api/v1/roles", headers=_auth(owner), json={"name": system_role["name"].lower()}
    )
    assert dup.status_code == 409

    unknown = await client.post(
        "/api/v1/roles",
        headers=_auth(owner),
        json={"name": "Weird", "permission_codes": ["nope.nope"]},
    )
    assert unknown.status_code == 400

    created = await client.post(
        "/api/v1/roles", headers=_auth(owner), json={"name": "Temp", "permission_codes": []}
    )
    assert (
        await client.delete(f"/api/v1/roles/{created.json()['id']}", headers=_auth(owner))
    ).status_code == 204


async def test_cannot_grant_permissions_you_lack_and_roles_are_tenant_isolated(
    client: AsyncClient,
) -> None:
    owner = await _signup(client, slug="esc-co", email="owner@escco.com")
    other = await _signup(client, slug="esc-other", email="owner@escother.com")

    manager_role = await client.post(
        "/api/v1/roles",
        headers=_auth(owner),
        json={"name": "RoleAdmin", "permission_codes": ["users.read", "users.manage"]},
    )
    assert manager_role.status_code == 201
    invited = await client.post(
        "/api/v1/users/invite",
        headers=_auth(owner),
        json={"email": "ra@escco.com", "full_name": "RA", "role_name": "RoleAdmin"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "ra@escco.com", "password": invited.json()["temporary_password"]},
    )
    role_admin = login.json()["tokens"]

    escalate = await client.post(
        "/api/v1/roles",
        headers=_auth(role_admin),
        json={"name": "Sneaky", "permission_codes": ["calls.manage"]},
    )
    assert escalate.status_code == 403

    other_roles = (await client.get("/api/v1/roles", headers=_auth(other))).json()
    assert "RoleAdmin" not in {r["name"] for r in other_roles}
    assert (
        await client.patch(
            f"/api/v1/roles/{manager_role.json()['id']}",
            headers=_auth(other),
            json={"name": "Hijack"},
        )
    ).status_code == 404
