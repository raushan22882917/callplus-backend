"""Permission codes and the default system roles that grant them.

Consumed by the seed data migration (migrations/versions) which inserts rows
so this stays the single source of truth for what "ADMIN" etc. actually means.
"""


class Permissions:
    CALLS_READ = "calls.read"
    CALLS_MANAGE = "calls.manage"
    CUSTOMERS_READ = "customers.read"
    CUSTOMERS_MANAGE = "customers.manage"
    TICKETS_READ = "tickets.read"
    TICKETS_MANAGE = "tickets.manage"
    AGENTS_READ = "agents.read"
    AGENTS_MANAGE = "agents.manage"
    WORKFLOWS_READ = "workflows.read"
    WORKFLOWS_MANAGE = "workflows.manage"
    CAMPAIGNS_READ = "campaigns.read"
    CAMPAIGNS_MANAGE = "campaigns.manage"
    CONVERSATIONS_READ = "conversations.read"
    CONVERSATIONS_MANAGE = "conversations.manage"
    INTEGRATIONS_READ = "integrations.read"
    INTEGRATIONS_MANAGE = "integrations.manage"
    PAYMENTS_READ = "payments.read"
    PAYMENTS_MANAGE = "payments.manage"
    ANALYTICS_READ = "analytics.read"
    BILLING_MANAGE = "billing.manage"
    SETTINGS_MANAGE = "settings.manage"
    USERS_READ = "users.read"
    USERS_MANAGE = "users.manage"
    AUDIT_READ = "audit.read"
    ASSISTANT_USE = "assistant.use"
    MCP_MANAGE = "mcp.manage"

    @classmethod
    def all(cls) -> list[str]:
        return [
            value
            for name, value in vars(cls).items()
            if not name.startswith("_") and isinstance(value, str)
        ]


ALL_PERMISSIONS_DESCRIPTIONS: dict[str, str] = {
    Permissions.CALLS_READ: "View call records and transcripts",
    Permissions.CALLS_MANAGE: "Manage/transfer/end calls",
    Permissions.CUSTOMERS_READ: "View customer records",
    Permissions.CUSTOMERS_MANAGE: "Create/edit customer records",
    Permissions.TICKETS_READ: "View support tickets",
    Permissions.TICKETS_MANAGE: "Create/edit/resolve support tickets",
    Permissions.AGENTS_READ: "View AI agent configuration",
    Permissions.AGENTS_MANAGE: "Configure AI agents and their tools",
    Permissions.WORKFLOWS_READ: "View automation workflows",
    Permissions.WORKFLOWS_MANAGE: "Configure automation workflows",
    Permissions.CAMPAIGNS_READ: "View outbound campaigns",
    Permissions.CAMPAIGNS_MANAGE: "Create/run outbound voice or WhatsApp campaigns",
    Permissions.CONVERSATIONS_READ: "View call transcripts and WhatsApp conversations",
    Permissions.CONVERSATIONS_MANAGE: "Send WhatsApp messages, connect WhatsApp numbers",
    Permissions.INTEGRATIONS_READ: "View connected integrations",
    Permissions.INTEGRATIONS_MANAGE: "Connect/disconnect third-party integrations",
    Permissions.PAYMENTS_READ: "View payment links and payments",
    Permissions.PAYMENTS_MANAGE: "Create payment links for customers",
    Permissions.ANALYTICS_READ: "View analytics and reports",
    Permissions.BILLING_MANAGE: "View/manage billing and invoices",
    Permissions.SETTINGS_MANAGE: "Manage tenant settings",
    Permissions.USERS_READ: "View team members",
    Permissions.USERS_MANAGE: "Invite/remove team members and assign roles",
    Permissions.AUDIT_READ: "View the audit log of sensitive tenant actions",
    Permissions.ASSISTANT_USE: "Use the internal AI assistant",
    Permissions.MCP_MANAGE: "Create/revoke API keys for external MCP clients (ChatGPT, Claude)",
}

# System role name -> permission codes. Roles are created with tenant_id=NULL
# so a single row is shared by every tenant.
DEFAULT_SYSTEM_ROLES: dict[str, list[str]] = {
    "OWNER": Permissions.all(),
    "ADMIN": [
        Permissions.CALLS_READ,
        Permissions.CALLS_MANAGE,
        Permissions.CUSTOMERS_READ,
        Permissions.CUSTOMERS_MANAGE,
        Permissions.TICKETS_READ,
        Permissions.TICKETS_MANAGE,
        Permissions.AGENTS_READ,
        Permissions.AGENTS_MANAGE,
        Permissions.WORKFLOWS_READ,
        Permissions.WORKFLOWS_MANAGE,
        Permissions.CAMPAIGNS_READ,
        Permissions.CAMPAIGNS_MANAGE,
        Permissions.CONVERSATIONS_READ,
        Permissions.CONVERSATIONS_MANAGE,
        Permissions.INTEGRATIONS_READ,
        Permissions.INTEGRATIONS_MANAGE,
        Permissions.PAYMENTS_READ,
        Permissions.PAYMENTS_MANAGE,
        Permissions.ANALYTICS_READ,
        Permissions.SETTINGS_MANAGE,
        Permissions.USERS_READ,
        Permissions.USERS_MANAGE,
        Permissions.AUDIT_READ,
        Permissions.ASSISTANT_USE,
        Permissions.MCP_MANAGE,
    ],
    "MANAGER": [
        Permissions.CALLS_READ,
        Permissions.CALLS_MANAGE,
        Permissions.CUSTOMERS_READ,
        Permissions.CUSTOMERS_MANAGE,
        Permissions.TICKETS_READ,
        Permissions.TICKETS_MANAGE,
        Permissions.AGENTS_READ,
        Permissions.WORKFLOWS_READ,
        Permissions.CAMPAIGNS_READ,
        Permissions.CAMPAIGNS_MANAGE,
        Permissions.CONVERSATIONS_READ,
        Permissions.CONVERSATIONS_MANAGE,
        Permissions.PAYMENTS_READ,
        Permissions.PAYMENTS_MANAGE,
        Permissions.ANALYTICS_READ,
        Permissions.USERS_READ,
        Permissions.ASSISTANT_USE,
    ],
    "SUPERVISOR": [
        Permissions.CALLS_READ,
        Permissions.CALLS_MANAGE,
        Permissions.CUSTOMERS_READ,
        Permissions.TICKETS_READ,
        Permissions.TICKETS_MANAGE,
        Permissions.AGENTS_READ,
        Permissions.CONVERSATIONS_READ,
        Permissions.CONVERSATIONS_MANAGE,
        Permissions.ANALYTICS_READ,
        Permissions.ASSISTANT_USE,
    ],
    "AGENT": [
        Permissions.CALLS_READ,
        Permissions.CUSTOMERS_READ,
        Permissions.TICKETS_READ,
        Permissions.TICKETS_MANAGE,
        Permissions.CONVERSATIONS_READ,
        Permissions.CONVERSATIONS_MANAGE,
        Permissions.PAYMENTS_MANAGE,
        Permissions.ASSISTANT_USE,
    ],
    "ANALYST": [
        Permissions.CALLS_READ,
        Permissions.CUSTOMERS_READ,
        Permissions.CONVERSATIONS_READ,
        Permissions.ANALYTICS_READ,
        Permissions.ASSISTANT_USE,
    ],
    "BILLING": [
        Permissions.BILLING_MANAGE,
        Permissions.PAYMENTS_READ,
        Permissions.ANALYTICS_READ,
        Permissions.ASSISTANT_USE,
    ],
}
