"""Every model, by sub-domain; import them from here.

This was one module of 2,400 lines. Each sub-module is one area of the
platform, and they are imported in an order where each needs only those
before it. `from platform_core.models import X` is the only import path
anything should use, which is why every name is re-exported below.
"""

from .accounts import (  # noqa: F401
    ApiToken,
    Application,
    ApplicationFeature,
    ApplicationGrant,
    ApplicationLogo,
    AuditEvent,
    Authenticator,
    Branding,
    ExternalIdentity,
    FeatureSwitch,
    Invitation,
    ManagedCredential,
    NamedResource,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    ProvisionedUser,
    RecoveryCode,
    SignInIdentity,
    User,
    unique_slug,
)
from .ai import (  # noqa: F401
    AI_PROVIDERS,
    AI_PURPOSES,
    AIAdmission,
    AIBudget,
    AIConfiguration,
    AIUsage,
)
from .chat import (  # noqa: F401
    CHAT_MODES,
    MESSAGE_ROLES,
    MESSAGE_STATUSES,
    ChatConversation,
    ChatMessage,
    ChatRetention,
    ChatSubmission,
)
from .code_graph import (  # noqa: F401
    AI_STEP_STATUSES,
    CodeDocumentation,
    CodeFile,
    CodeRelationship,
    CodeRepository,
    CodeReview,
    CodeReviewFile,
    CodeSnapshot,
)
from .factory import (  # noqa: F401
    ITEM_CATEGORIES,
    PHASES,
    RUBRIC,
    ChangePlan,
    FactoryRun,
    PlanItem,
    ProposedChange,
    RunEvent,
    RunPhase,
)
from .knowledge import (  # noqa: F401
    CHUNK_FIELDS,
    CONNECTOR_KINDS,
    GRAPH_FIELDS,
    BenchmarkQuestion,
    BenchmarkRun,
    Connector,
    Document,
    DocumentAIResult,
    Embedding,
    GraphElement,
    GraphRevision,
    KnowledgeChunk,
    KnowledgeEntry,
    KnowledgeEntryQuerySet,
    KnowledgeGraph,
    KnowledgeSource,
    PersonalDataPolicy,
)
from .operations import (  # noqa: F401
    AutomationConnection,
    AutomationRun,
    IncidentProfile,
    OperationsGraph,
    OpsEdge,
    OpsNode,
    ServiceOpsTestRun,
    TriageHypothesis,
    TriageRun,
    TriageVerdict,
)
from .runtime import (  # noqa: F401
    Job,
    LaneHeartbeat,
    StreamLease,
)
