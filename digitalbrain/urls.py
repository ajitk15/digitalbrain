from django.contrib.auth import views as auth
from django.urls import path

from platform_core import (
    administration,
    ai,
    api,
    code_graph,
    connectors,
    documents,
    graphs,
    views,
    workbench,
)
from platform_core.utility import api_chat

urlpatterns = [
    path("applications/<uuid:pk>/graph/", graphs.graph_view, name="graph"),
    path(
        "applications/<uuid:pk>/graph/generate/",
        graphs.graph_generate,
        name="graph-generate",
    ),
    path("applications/<uuid:pk>/code-graph/", code_graph.code_graph, name="code-graph"),
    path(
        "applications/<uuid:pk>/code-graph/files/<uuid:file_id>/",
        code_graph.code_file,
        name="code-file",
    ),
    path(
        "applications/<uuid:pk>/documents/<uuid:document_id>/delete/",
        documents.document_delete,
        name="document-delete",
    ),
    path("applications/<uuid:pk>/ai/", ai.ai_settings, name="ai-settings"),
    path("applications/<uuid:pk>/knowledge/", workbench.knowledge, name="knowledge"),
    path(
        "applications/<uuid:pk>/knowledge/<uuid:entry_id>/",
        workbench.knowledge_detail,
        name="knowledge-detail",
    ),
    # Machine-facing surfaces. Bearer-token only: no session cookie is accepted,
    # which is what keeps them out of reach of an authenticated browser.
    path(
        "api/v1/applications/<str:reference>/graph/search/",
        api.graph_search,
        name="api-graph-search",
    ),
    path("api/v1/applications/<str:reference>/mcp/", api.mcp, name="api-mcp"),
    # The one API surface that calls a model. Gated on the `chat_api` feature
    # switch, which an owner has to turn on.
    path("api/v1/applications/<str:reference>/chat/", api_chat.chat, name="api-chat"),
    path(
        "api/v1/applications/<str:reference>/chat/<uuid:message_id>/stream/",
        api_chat.chat_stream,
        name="api-chat-stream",
    ),
    path("applications/<uuid:pk>/api-access/", views.api_tokens, name="api-tokens"),
    path("applications/<uuid:pk>/chat/", workbench.chat, name="chat"),
    path(
        "applications/<uuid:pk>/chat/conversations/<uuid:conversation_id>/",
        workbench.chat_conversation,
        name="chat-conversation",
    ),
    path(
        "applications/<uuid:pk>/chat/messages/<uuid:message_id>/regenerate/",
        workbench.chat_regenerate,
        name="chat-regenerate",
    ),
    path(
        "applications/<uuid:pk>/chat/messages/<uuid:message_id>/edit/",
        workbench.chat_edit,
        name="chat-edit",
    ),
    path(
        "applications/<uuid:pk>/chat/messages/<uuid:message_id>/stream/",
        workbench.chat_stream,
        name="chat-stream",
    ),
    path(
        "applications/<uuid:pk>/chat/messages/<uuid:message_id>/stop/",
        workbench.chat_stop,
        name="chat-stop",
    ),
    path(
        "applications/<uuid:pk>/chat/messages/<uuid:message_id>/fragment/",
        workbench.chat_message,
        name="chat-message",
    ),
    path("applications/<uuid:pk>/plans/", workbench.plans, name="plans"),
    path("applications/<uuid:pk>/plans/<uuid:plan_id>/", workbench.plan_detail, name="plan-detail"),
    path("applications/<uuid:pk>/connectors/", connectors.connectors, name="connectors"),
    path("platform/users/", administration.users, name="users"),
    path("platform/users/<uuid:pk>/status/", administration.user_status, name="user-status"),
    path(
        "platform/organizations/<uuid:pk>/status/",
        administration.organization_status,
        name="organization-status",
    ),
    path(
        "applications/<uuid:pk>/status/",
        administration.application_status,
        name="application-status",
    ),
    path("health/live", views.live, name="live"),
    path("health/ready", views.ready, name="ready"),
    path("accounts/login/", auth.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth.LogoutView.as_view(), name="logout"),
    path(
        "accounts/password/",
        administration.RequiredPasswordChangeView.as_view(),
        name="password",
    ),
    path("", views.dashboard, name="dashboard"),
    path("branding/logo.png", views.logo, name="logo"),
    path("platform/", views.platform_console, name="platform-console"),
    path("platform/branding/", views.branding, name="branding"),
    path("platform/features/", views.features, name="features"),
    path("audit/", views.audit_log, name="audit"),
    path("applications/<uuid:pk>/usage/", views.usage, name="usage"),
    path("applications/<uuid:pk>/features/", views.features, name="application-features"),
    path("applications/<uuid:pk>/chat-settings/", views.chat_settings, name="chat-settings"),
    path(
        "applications/<uuid:pk>/connectors/new/",
        connectors.connector_form,
        name="connector-new",
    ),
    path(
        "applications/<uuid:pk>/connectors/<uuid:connector_id>/",
        connectors.connector_form,
        name="connector-edit",
    ),
    path("organizations/new/", views.create_organization, name="organization-new"),
    path("organizations/<uuid:pk>/", views.organization, name="organization"),
    path(
        "organizations/<uuid:pk>/members/", views.organization_members, name="organization-members"
    ),
    path("organizations/<uuid:pk>/portfolios/new/", views.create_portfolio, name="portfolio-new"),
    path("portfolios/<uuid:pk>/products/new/", views.create_product, name="product-new"),
    path("products/<uuid:pk>/applications/new/", views.create_application, name="application-new"),
    # The one-step route: no product yet, so the form chooses or names the
    # portfolio and product alongside the application.
    path(
        "organizations/<uuid:organization_id>/applications/new/",
        views.create_application,
        name="application-create",
    ),
    path("applications/<uuid:pk>/", views.application_home, name="application"),
    path("applications/<uuid:pk>/documents/", documents.documents, name="documents"),
    path(
        "applications/<uuid:pk>/documents/<uuid:document_id>/",
        documents.document_detail,
        name="document-detail",
    ),
    path("applications/<uuid:pk>/access/", views.application_access, name="application-access"),
]

handler500 = "platform_core.views.server_error"
