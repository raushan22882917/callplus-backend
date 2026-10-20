"""The knowledge document type must be written the way the migration created it.

Tests build tables from the models, so they never see a mismatch with the real
(migrated) database; this reads the migration to catch one.
"""

import re
from pathlib import Path

from packages.database.models import KnowledgeDocument

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "b7c1e4a9d3f2_add_knowledge_base.py"


def test_document_source_labels_match_the_migration():
    declared = re.search(r'ENUM\(((?:"[a-z_]+",\s*)+)name="knowledge_document_source"', MIGRATION.read_text())
    assert declared, "could not find the knowledge_document_source enum in the migration"
    in_database = re.findall(r'"([a-z_]+)"', declared.group(1))
    assert in_database == ["text", "upload"]
    assert list(KnowledgeDocument.__table__.c.source.type.enums) == in_database


async def test_a_document_can_be_added_and_read_back(client):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": "Kb Co", "tenant_slug": "kb-co", "full_name": "O", "email": "o@kb-co.com", "password": "supersecret1"},
    )
    auth = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    made = await client.post("/api/v1/knowledge/documents", headers=auth, json={"title": "Hours", "content": "Open 10 am to 8 pm every day."})
    assert made.status_code == 201, made.text
    listed = (await client.get("/api/v1/knowledge/documents", headers=auth)).json()
    assert [d["title"] for d in listed] == ["Hours"] and listed[0]["source"] == "text"
