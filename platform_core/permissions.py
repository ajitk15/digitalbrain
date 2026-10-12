"""Source-system permissions on answers: cite a record only to who may open it.

An application grant decides who can use an application. It does not say who
may open each Jira issue in it: a connector reads with one service account, so
everything that account can browse was citable to every member. With a
connector's "Enforce source permissions" on, each record it imports carries
the platform users the source system lets open it, and every answer path
filters by the person asking:

- graph citations and keyword search, including what embeddings found;
- the chat tools (search, fetch, citation checks) and the API;
- Code Factory, as the run's requester, and its ticket picker.

**Fail closed.** A record from an enforcing connector starts with nobody
allowed and stays invisible to AI until its first check. A caller with no user
(ServiceOps' page-level evidence, the benchmark's automatic run) sees only
records with no list at all. A member is allowed only if their account could
be found and the system said yes; a lookup that fails, a hidden email address,
a check past the per-sync cap - each leaves them out, never in.

What it does not cover, deliberately stated: the Sources list, a source's own
page and the graph explorer still show every record, in full, to every member
of the application, as before - this is about what answers cite and what is
sent to a model, and the connector form says so where the option is ticked.
Treating a source system's permissions as a confidentiality boundary would
mean enforcing them on those pages and downloads too; that is a product
decision, not an omission. ServiceNow and SharePoint
are not checked: ServiceNow ACLs are scripts evaluated per user and SharePoint
reads as the application, and both need delegated per-user access this
platform does not have. A connector of those kinds cannot switch it on.

Jira: each member is matched to an account by email (Cloud
`user/search?query`, Data Center `user/search?username`), remembered in
`ExternalIdentity`, and then asked of each issue with
`user/viewissue/search` - "can this account browse this issue". Issues imported
or changed in a sync, and those not checked for `RECHECK_HOURS`, are checked,
at most `MAX_CHECKS` questions a sync.
"""

import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Kinds whose permissions can be checked. Only these offer the switch.
SUPPORTED = {"jira"}
#: Questions asked of Jira per sync: members x issues. A board of a hundred
#: issues and twenty members fits; a bigger one is finished by later syncs.
MAX_CHECKS = 2000
#: How long a check stands before it is asked again. Someone removed from a
#: Jira project stops seeing its issues in answers within a day.
RECHECK_HOURS = 24


class Everyone:
    """The viewer for a measurement that answers nobody: the benchmark."""


EVERYONE = Everyone()


def permitted(entry, viewer):
    """Whether this person may be shown this source in an answer."""
    allowed = entry.access_principals
    if allowed is None or viewer is EVERYONE:
        return True
    return viewer is not None and str(viewer.pk) in allowed


def only_permitted(entries, viewer):
    return [entry for entry in entries if permitted(entry, viewer)]


# ── Jira ──────────────────────────────────────────────────────────────────────


def issue_key(url):
    """The issue key at the end of a /browse/ address, or ""."""
    _, marker, key = url.rpartition("/browse/")
    return key if marker and key and "/" not in key else ""


def jira_account(user, config, headers):
    """The member's Jira account id, found by email once and then remembered."""
    from .connector_kinds import api_json
    from .models import ExternalIdentity

    site = config["base_url"]
    known = ExternalIdentity.objects.filter(user=user, system="jira", site=site).first()
    if known and (known.method == "manual" or known.resolved_at > _stale()):
        return known.account
    account = ""
    email = (user.email or "").strip().casefold()
    if email:
        cloud = config.get("auth") == "cloud"
        url = (
            f"{site}/rest/api/3/user/search?{urlencode({'query': email})}"
            if cloud
            else f"{site}/rest/api/2/user/search?{urlencode({'username': email})}"
        )
        found = api_json(url, headers, label="Jira")
        matches = [
            person
            for person in (found if isinstance(found, list) else [])
            if isinstance(person, dict) and str(person.get("emailAddress", "")).casefold() == email
        ]
        # Exactly one, matched on the whole address. Hidden addresses or two
        # people answering is no answer: the member is left out.
        if len(matches) == 1:
            account = str(matches[0].get("accountId" if cloud else "name") or "")
    ExternalIdentity.objects.update_or_create(
        user=user,
        system="jira",
        site=site,
        defaults={"account": account, "method": "email"},
    )
    return account


def jira_can_browse(config, headers, key, account):
    from .connector_kinds import api_json

    field = "accountId" if config.get("auth") == "cloud" else "username"
    version = "3" if config.get("auth") == "cloud" else "2"
    query = urlencode({field: account, "issueKey": key, "maxResults": 1})
    found = api_json(
        f"{config['base_url']}/rest/api/{version}/user/viewissue/search?{query}",
        headers,
        label="Jira",
    )
    return isinstance(found, list) and any(
        isinstance(person, dict)
        and str(person.get("accountId" if field == "accountId" else "name")) == account
        for person in found
    )


def _stale():
    return timezone.now() - timedelta(hours=RECHECK_HOURS)


def sync_jira(connector, app, secret):
    """Check who may open this connector's issues. Returns {checked, allowed, unmatched}.

    Network calls happen here, outside any transaction; each issue's list is
    written when its checks finish, so a failure part way keeps what was done
    and leaves the rest closed.
    """
    from .connector_kinds import jira_headers, jira_namespace
    from .models import ApplicationGrant, KnowledgeEntry

    config = connector.config
    headers = jira_headers(config, secret)
    namespace = jira_namespace(config)
    members = [
        grant.user
        for grant in ApplicationGrant.objects.filter(application=app, user__is_active=True)
        .select_related("user")
        .order_by("user_id")
    ]
    accounts, unmatched = {}, []
    for member in members:
        account = jira_account(member, config, headers)
        if account:
            accounts[member.pk] = account
        else:
            unmatched.append(member.username)
    due = (
        KnowledgeEntry.objects.filter(application=app, active=True, source__startswith=namespace)
        .exclude(access_checked_at__gt=_stale())
        .order_by("access_checked_at", "created_at")
    )
    asked = checked = 0
    for entry in due:
        key = issue_key(entry.source)
        if not key:
            continue
        if asked + len(accounts) > MAX_CHECKS:
            break
        allowed = []
        for user_id, account in accounts.items():
            asked += 1
            if jira_can_browse(config, headers, key, account):
                allowed.append(str(user_id))
        KnowledgeEntry.objects.filter(pk=entry.pk).update(
            access_principals=sorted(allowed), access_checked_at=timezone.now()
        )
        checked += 1
    return {"checked": checked, "asked": asked, "unmatched": unmatched}


def enforce(connector, app, enabled):
    """Turn enforcement on or off for a connector's existing records.

    On, every record it imported becomes invisible to AI until checked - not
    visible until someone notices. Off, its records go back to "not checked".
    """
    from .connector_kinds import KINDS
    from .models import KnowledgeEntry

    try:
        namespace = KINDS[connector.kind].namespace(connector.config)
    except KeyError:
        return
    records = KnowledgeEntry.objects.filter(application=app, source__startswith=namespace)
    if enabled:
        records.filter(access_principals__isnull=True).update(
            access_principals=[], access_checked_at=None
        )
    else:
        records.update(access_principals=None, access_checked_at=None)


def check_after_sync(connector, app, secret):
    """The permission step of an import, recorded on the connector, never raising."""
    from .models import Connector

    if not connector.enforce_permissions or connector.kind not in SUPPORTED:
        return None
    try:
        result = sync_jira(connector, app, secret)
    except ValidationError as failure:
        message = "Permission check failed: " + " ".join(failure.messages)
        Connector.objects.filter(pk=connector.pk).update(last_error=message[:500])
        logger.warning("permission_sync_failed", extra={"event": "permission_sync_failed"})
        return None
    if result["unmatched"]:
        Connector.objects.filter(pk=connector.pk).update(
            last_error=(
                "No Jira account found for "
                + ", ".join(result["unmatched"][:5])
                + ("..." if len(result["unmatched"]) > 5 else "")
                + ": they are not shown these issues in answers."
            )[:500]
        )
    return result
