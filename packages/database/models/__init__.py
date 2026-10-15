"""Import every model here so Base.metadata is complete for Alembic autogenerate."""

from packages.database.models.agent import AgentStatus, AIAgent, AIAgentTool, AIAgentVersion
from packages.database.models.agent_test import AgentTestCase, AgentTestRun
from packages.database.models.audit import AuditLog
from packages.database.models.billing import (
    BillingInterval,
    Invoice,
    InvoiceStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from packages.database.models.blocklist import BlockedCaller
from packages.database.models.call import Call, CallDirection, CallStatus
from packages.database.models.campaign import (
    Campaign,
    CampaignChannel,
    CampaignContact,
    CampaignContactStatus,
    CampaignStatus,
)
from packages.database.models.conversation import (
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    MessageSender,
)
from packages.database.models.customer import Customer, CustomerActivity
from packages.database.models.developer import (
    ApiKey,
    ApiKeyUsageDaily,
    IdempotencyRecord,
    WebhookDelivery,
    WebhookEndpoint,
)
from packages.database.models.integration import (
    Integration,
    IntegrationCredential,
    IntegrationStatus,
)
from packages.database.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentSource,
)
from packages.database.models.mcp import McpApiKey
from packages.database.models.mcp_oauth import McpOAuthClient, McpOAuthCode, McpOAuthToken
from packages.database.models.payment import (
    Payment,
    PaymentLink,
    PaymentLinkStatus,
    PaymentStatus,
)
from packages.database.models.rbac import (
    MembershipStatus,
    Permission,
    Role,
    TenantUser,
    role_permissions,
)
from packages.database.models.telephony import CallEvent, PhoneNumber
from packages.database.models.tenant import Tenant, TenantStatus
from packages.database.models.ticket import Ticket, TicketComment, TicketPriority, TicketStatus
from packages.database.models.transfer_target import TransferTarget
from packages.database.models.user import User
from packages.database.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from packages.database.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTask,
    WorkflowTrigger,
)

from packages.database.models.crm import CrmCompany, CrmLead, CrmDeal, CrmTask, CrmNote, CrmStage, CrmTaskType, CrmTaskStatus  # noqa: E402
from packages.database.models.guardrails import GuardrailRule, GuardrailEvent, KnowledgeAIControl  # noqa: E402
from packages.database.models.workspace import AgentProfile, HumanAgentStatus, BusinessHours, Holiday, CallHandoff, HandlerState  # noqa: E402
from packages.database.models.insights import QAScorecard, CallQAScore  # noqa: E402
from packages.database.models.operations import TicketCategory, SLAPolicy, TicketSLA, BookableService, AppointmentResource, Appointment, FollowUp, PaymentPromise  # noqa: E402
from packages.database.models.channels import ChannelSettings, ChannelThread, MissedCall, Voicemail  # noqa: E402
from packages.database.models.telephony_plus import IvrMenu, CallQueue, RingGroup, CallForwardingRule, DndEntry, CampaignDialerSettings, CampaignContactState, CampaignOutcome, SipTrunk, NumberRouting, ParkedCall  # noqa: E402

from packages.database.models.platform import UserTotp, PlatformAdmin, TenantSecuritySettings, RetentionPolicy, SsoConfig, UsageEvent, PricingPlan, IntegrationConnector  # noqa: E402
from packages.database.models.oauth import OAuthConnection, OAuthConnectionStatus, OAuthState  # noqa: E402

__all__ = [
    "ApiKey",
    "ApiKeyUsageDaily",
    "IdempotencyRecord",
    "WebhookDelivery",
    "WebhookEndpoint",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeDocumentSource",
    "AgentStatus",
    "AIAgent",
    "AIAgentTool",
    "AIAgentVersion",
    "AgentTestCase",
    "AgentTestRun",
    "AuditLog",
    "BillingInterval",
    "BlockedCaller",
    "Call",
    "CallDirection",
    "CallEvent",
    "CallStatus",
    "Campaign",
    "CampaignChannel",
    "CampaignContact",
    "CampaignContactStatus",
    "CampaignStatus",
    "Conversation",
    "ConversationChannel",
    "Customer",
    "CustomerActivity",
    "Integration",
    "IntegrationCredential",
    "IntegrationStatus",
    "Invoice",
    "InvoiceStatus",
    "McpApiKey",
    "McpOAuthClient",
    "McpOAuthCode",
    "McpOAuthToken",
    "MembershipStatus",
    "Message",
    "MessageDirection",
    "MessageSender",
    "Payment",
    "PaymentLink",
    "PaymentLinkStatus",
    "PaymentStatus",
    "Permission",
    "PhoneNumber",
    "Plan",
    "Role",
    "TenantUser",
    "role_permissions",
    "Subscription",
    "SubscriptionStatus",
    "Tenant",
    "TransferTarget",
    "TenantStatus",
    "Ticket",
    "TicketComment",
    "TicketPriority",
    "TicketStatus",
    "User",
    "WhatsAppAccount",
    "WhatsAppTemplate",
    "Workflow",
    "WorkflowRun",
    "WorkflowRunStatus",
    "WorkflowTask",
    "WorkflowTrigger",
    "CrmCompany",
    "CrmLead",
    "CrmDeal",
    "CrmTask",
    "CrmNote",
    "CrmStage",
    "CrmTaskType",
    "CrmTaskStatus",
    "GuardrailRule",
    "GuardrailEvent",
    "KnowledgeAIControl",
    "AgentProfile",
    "HumanAgentStatus",
    "BusinessHours",
    "Holiday",
    "CallHandoff",
    "HandlerState",
    "QAScorecard",
    "CallQAScore",
    "TicketCategory",
    "SLAPolicy",
    "TicketSLA",
    "BookableService",
    "AppointmentResource",
    "Appointment",
    "FollowUp",
    "PaymentPromise",
    "ChannelSettings",
    "ChannelThread",
    "MissedCall",
    "Voicemail",
    "IvrMenu",
    "CallQueue",
    "RingGroup",
    "CallForwardingRule",
    "DndEntry",
    "CampaignDialerSettings",
    "CampaignContactState",
    "CampaignOutcome",
    "SipTrunk",
    "NumberRouting",
    "ParkedCall",
    "UserTotp",
    "PlatformAdmin",
    "TenantSecuritySettings",
    "RetentionPolicy",
    "SsoConfig",
    "UsageEvent",
    "PricingPlan",
    "IntegrationConnector",
    "OAuthConnection",
    "OAuthConnectionStatus",
    "OAuthState",
]
