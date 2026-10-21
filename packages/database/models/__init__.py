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
from packages.database.models.channels import (  # noqa: E402
    ChannelSettings,
    ChannelThread,
    MissedCall,
    Voicemail,
)
from packages.database.models.conversation import (
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    MessageSender,
)
from packages.database.models.cost_controls import CostAlert, CostControls
from packages.database.models.crm import (  # noqa: E402
    CrmCompany,
    CrmDeal,
    CrmLead,
    CrmNote,
    CrmStage,
    CrmTask,
    CrmTaskStatus,
    CrmTaskType,
)
from packages.database.models.csat import CsatSettings, CsatSurvey
from packages.database.models.customer import Customer, CustomerActivity
from packages.database.models.developer import (
    ApiKey,
    ApiKeyUsageDaily,
    IdempotencyRecord,
    WebhookDelivery,
    WebhookEndpoint,
)
from packages.database.models.guardrails import (  # noqa: E402
    GuardrailEvent,
    GuardrailRule,
    KnowledgeAIControl,
)
from packages.database.models.insights import CallQAScore, QAScorecard  # noqa: E402
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
from packages.database.models.oauth import (  # noqa: E402
    OAuthConnection,
    OAuthConnectionStatus,
    OAuthState,
)
from packages.database.models.operations import (  # noqa: E402
    Appointment,
    AppointmentResource,
    BookableService,
    FollowUp,
    PaymentPromise,
    SLAPolicy,
    TicketCategory,
    TicketSLA,
)
from packages.database.models.payment import (
    Payment,
    PaymentLink,
    PaymentLinkStatus,
    PaymentStatus,
)
from packages.database.models.platform import (  # noqa: E402
    IntegrationConnector,
    PlatformAdmin,
    PricingPlan,
    RetentionPolicy,
    SsoConfig,
    TenantSecuritySettings,
    UsageEvent,
    UserTotp,
)
from packages.database.models.post_call import CallNotification, PostCallSettings
from packages.database.models.privacy import (
    ConsentRecord,
    DataBreach,
    PrivacyProfile,
    PrivacyRequest,
)
from packages.database.models.rbac import (
    MembershipStatus,
    Permission,
    Role,
    TenantUser,
    role_permissions,
)
from packages.database.models.session import UserSession
from packages.database.models.telephony import CallEvent, PhoneNumber
from packages.database.models.telephony_plus import (  # noqa: E402
    CallForwardingRule,
    CallQueue,
    CampaignContactState,
    CampaignDialerSettings,
    CampaignOutcome,
    DndEntry,
    IvrMenu,
    NumberRouting,
    ParkedCall,
    RingGroup,
    SipTrunk,
)
from packages.database.models.tenant import Tenant, TenantStatus
from packages.database.models.ticket import Ticket, TicketComment, TicketPriority, TicketStatus
from packages.database.models.transfer_target import TransferTarget
from packages.database.models.user import User
from packages.database.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from packages.database.models.widget_lead import WidgetLead
from packages.database.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTask,
    WorkflowTrigger,
)
from packages.database.models.workspace import (  # noqa: E402
    AgentProfile,
    BusinessHours,
    CallHandoff,
    HandlerState,
    Holiday,
    HumanAgentStatus,
)

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
    "CallNotification",
    "ConsentRecord",
    "CostAlert",
    "DataBreach",
    "PrivacyProfile",
    "PrivacyRequest",
    "PostCallSettings",
    "CostControls",
    "CsatSettings",
    "UserSession",
    "WidgetLead",
    "CsatSurvey",
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
