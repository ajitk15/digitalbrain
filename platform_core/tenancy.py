"""A request scoped to one application sees only that application's rows.

The first layer is the code: every view resolves its application through
`policy.application_for` and filters by it, and agent tools are bound to one
application before a run starts. This is the second, in the database: PostgreSQL
row-level security on every table that carries a required `application_id`
(migration 0091), keyed on a session setting this module binds.

* A request under ``applications/<pk>/`` is bound to that ``pk`` before its view
  runs (`TenantMiddleware`), and an API request once its token has named its
  application (`api.authorize`). A query that forgot its ``application=``
  filter then reads another application's row as "not found", and a write
  for another application is refused - instead of either reaching the page.
* Nothing bound means nothing narrowed. Background lanes, management commands,
  migrations, the sign-in pages and the platform's own administration see
  every row, as they did. A binding never outlives its request: the middleware
  clears it, whatever the view did.

It defends against a missing filter, not against SQL injection: the setting is
the session's own, and a statement that can run SQL can clear it. The runtime
role being unable to change the schema (`database_roles`) is what limits that.

A superuser bypasses row security entirely, which is why it applies to the
runtime role in production; the policies are FORCEd as well, so a test suite
running as a non-superuser owner is held to them. SQLite has no row security:
there this module does nothing and the code is the only layer.
"""

from contextlib import contextmanager

from django.db import DatabaseError, connection

#: The session setting the policies read. A custom setting needs a dotted name.
SETTING = "digitalbrain.tenant"
#: Tables with a required application_id that are deliberately not narrowed,
#: because a request for one application reads them across applications:
#: grants and features (who may open which application, and the nav that lists
#: them), logos (drawn in that nav), and gateway admissions (the per-provider
#: cap counts every application's calls in flight).
UNSCOPED = {
    "ApplicationGrant",
    "ApplicationFeature",
    "ApplicationLogo",
    "AIAdmission",
}


def supported():
    return connection.vendor == "postgresql"


def bound():
    """The application this connection is narrowed to, or None."""
    return getattr(connection, "digitalbrain_tenant", None)


def bind(application_id):
    """Narrow this connection's tenant tables to one application."""
    if not supported():
        return
    value = str(application_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, false)", [SETTING, value])
    connection.digitalbrain_tenant = value


def clear():
    """Undo `bind`. Cheap when nothing is bound."""
    if not supported() or bound() is None:
        return
    connection.digitalbrain_tenant = None
    if connection.connection is None:
        # Closed since it was bound, and the setting went with the session.
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, '', false)", [SETTING])
    except DatabaseError:
        # A session that cannot be cleared must not serve the next request
        # still narrowed to this one's application: end it instead.
        connection.close()


@contextmanager
def scoped(application_id):
    """Bound for the block, then put back as it was."""
    before = bound()
    bind(application_id)
    try:
        yield
    finally:
        if before is None:
            clear()
        else:
            bind(before)


class TenantMiddleware:
    """Bind ``applications/<pk>/...`` requests to that application, and always clear."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        finally:
            clear()

    def process_view(self, request, view, args, kwargs):
        match = request.resolver_match
        if match is None or not request.path.startswith("/applications/"):
            return None
        pk = kwargs.get("pk")
        if pk is not None and match.route.startswith("applications/<uuid:pk>/"):
            bind(pk)
        return None
