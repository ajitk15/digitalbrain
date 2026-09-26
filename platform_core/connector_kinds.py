"""What each external system needs, and how its records become knowledge.

One entry per connector kind. `sync` in connectors.py stays kind-agnostic: it
reads the credential, asks the adapter for records, and writes them. Adding a
fourth system is an entry here, not a new import pipeline.

Two rules every adapter obeys, both from CLAUDE.md:

* Every outbound request goes through `fetching.fetch`, which resolves the host,
  refuses private, loopback, link-local and metadata addresses, pins the
  connection to the address it validated and follows no redirects. Jira and
  ServiceNow take a base URL from the user, so without this a connector would be
  a request forgery tool pointed at internal infrastructure.
* Credentials are read from a mounted file and never stored in `config`. Where a
  credential has two halves, the non-secret half (an account email, a client id)
  lives in `config` and only the secret half is mounted.
"""

import base64
import json
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import quote, urlencode

from django import forms
from django.core.exceptions import ValidationError

from .fetching import FetchError, fetch, normalise

#: Never import an unbounded backlog: a connector is a knowledge feed, not a mirror.
MAX_RECORDS = 100
MAX_BODY_CHARACTERS = 100_000


@dataclass(frozen=True)
class Record:
    """One external item, already reduced to what knowledge storage needs."""

    external_id: str
    title: str
    body: str
    url: str


def api_json(url, headers, *, label):
    """A JSON GET through the hardened fetcher."""
    try:
        body, _, _, _ = fetch(url, headers=headers)
    except FetchError as failure:
        refused = ValidationError(f"{label}: {failure}")
        # The status travels with the error so a caller can tell "this endpoint
        # no longer exists on this instance" from "this request failed".
        refused.http_status = failure.status
        raise refused from None
    try:
        return json.loads(body)
    except ValueError:
        raise ValidationError(f"{label} returned a response that was not JSON.") from None


def basic(user, secret):
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


def https_base(value):
    """A validated https origin for a user-supplied instance URL.

    Refuses an address that obviously points inside the network at configuration
    time, so the person is told immediately rather than at the first import. This
    is not the security boundary - `fetch` resolves and re-checks every address at
    request time, which also covers a host name that resolves somewhere private.
    Deliberately no DNS here: a form submission should not wait on a lookup.
    """
    try:
        parsed = normalise(value)
    except FetchError as failure:
        raise forms.ValidationError(str(failure)) from None
    if parsed.scheme != "https":
        raise forms.ValidationError("Use an https address.")
    host = parsed.hostname or ""
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise forms.ValidationError("That address is not reachable from this server.")
    if address is None and "." not in host:
        raise forms.ValidationError("Enter the full host name of your instance.")
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{host}{port}"


def named(value):
    """The `name` of a nested provider object, or "" for anything else.

    Providers wrap almost everything - a status, a priority, a type - in an
    object with a name. Reading it defensively keeps a malformed record from
    raising where it should simply be reported as unknown.

    ServiceNow is the exception: with `sysparm_display_value=true` a reference
    field arrives as `{"display_value": ..., "link": ...}` and has no name. Read
    only as a name, every incident's Service, CI and assignment group came
    through empty - and ServiceOps matches precedents and changes on exactly
    those, so triage lost its strongest signals without a word.
    """
    if isinstance(value, dict):
        return text(value.get("name") or value.get("display_value"))
    return text(value)


def header(pairs):
    """The state lines that precede a record's own text.

    Status belongs *in the body* rather than beside it: the stored content is
    what the digest is computed over, so a ticket moving to Done has to change
    the text or `sync` sees an identical record and imports nothing. Before this,
    a board could be re-imported all day and never reflect a single transition.
    """
    lines = [f"{label}: {value}" for label, value in pairs if value]
    return "\n".join(lines)


def incident_header(pairs):
    """Keep provider text on one line so it cannot masquerade as another field."""
    return header(
        (label, " ".join(value.split())[:2000] if value else "") for label, value in pairs
    )


def text(value):
    """A string, or "" for whatever else the provider put in that field.

    No length cap here on purpose: the one cap that matters is applied where the
    record is stored, in `connectors.sync`. A second limit here would only be a
    quieter place for the two numbers to disagree.
    """
    return value if isinstance(value, str) else ""


# --------------------------------------------------------------------- GitHub


class GitHubForm(forms.Form):
    repository = forms.RegexField(
        regex=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$",
        max_length=200,
        label="Repository",
        help_text="owner/repository. The most recently updated issues are imported.",
    )

    def clean_repository(self):
        value = self.cleaned_data["repository"]
        if value.split("/")[1] in {".", ".."}:
            raise forms.ValidationError("Enter an owner/repository.")
        return value


def github_records(config, secret):
    repository = config["repository"]
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    items = api_json(
        f"https://api.github.com/repos/{repository}/issues"
        f"?state=all&per_page={MAX_RECORDS}&sort=updated",
        headers,
        label="GitHub",
    )
    if not isinstance(items, list):
        raise ValidationError("GitHub returned an unexpected issue list.")
    records = []
    for item in items:
        # Pull requests come back from the issues endpoint; they are not issues.
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        if type(item.get("number")) is not int or not isinstance(item.get("title"), str):
            raise ValidationError("GitHub returned an unexpected issue record.")
        labels = item.get("labels")
        state = header(
            [
                ("Number", f"#{item['number']}"),
                ("State", text(item.get("state"))),
                (
                    "Labels",
                    " ".join(named(one) for one in labels) if isinstance(labels, list) else "",
                ),
                ("Updated", text(item.get("updated_at"))),
            ]
        )
        records.append(
            Record(
                external_id=str(item["number"]),
                title=item["title"][:200],
                body=f"{state}\n\n{text(item.get('body'))}".strip(),
                url=f"https://github.com/{repository}/issues/{item['number']}",
            )
        )
    return records


# ----------------------------------------------------------------------- Jira

JIRA_AUTH = [
    ("cloud", "Jira Cloud (account email + API token)"),
    ("datacenter", "Jira Data Center or Server (personal access token)"),
]


class JiraForm(forms.Form):
    base_url = forms.CharField(
        max_length=200,
        label="Site URL",
        help_text="For example https://your-team.atlassian.net",
    )
    auth = forms.ChoiceField(choices=JIRA_AUTH, label="Authentication", initial="cloud")
    account_email = forms.EmailField(
        max_length=200,
        required=False,
        label="Account email",
        help_text="Jira Cloud only. The token itself belongs on the Credentials screen, not here.",
    )
    jql = forms.CharField(
        max_length=400,
        required=False,
        label="JQL filter",
        help_text="Optional, for example project = OPS AND updated >= -30d",
    )

    def clean_base_url(self):
        return https_base(self.cleaned_data["base_url"])

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("auth") == "cloud" and not cleaned.get("account_email"):
            raise forms.ValidationError("Jira Cloud needs the account email the token belongs to.")
        return cleaned


def jira_records(config, secret):
    base = config["base_url"]
    if config.get("auth") == "cloud":
        headers = {"Authorization": basic(config["account_email"], secret)}
    else:
        headers = {"Authorization": f"Bearer {secret}"}
    headers["Accept"] = "application/json"
    query = urlencode(
        {
            "jql": config.get("jql") or "order by updated DESC",
            "maxResults": MAX_RECORDS,
            "fields": "summary,description,updated,status,priority,issuetype,labels",
        }
    )
    payload = jira_search(base, headers, query)
    issues = payload.get("issues") if isinstance(payload, dict) else None
    if not isinstance(issues, list):
        raise ValidationError("Jira returned an unexpected issue list.")
    records = []
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("key"), str):
            raise ValidationError("Jira returned an unexpected issue record.")
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        labels = fields.get("labels")
        state = header(
            [
                ("Key", issue["key"]),
                ("Type", named(fields.get("issuetype"))),
                ("Status", named(fields.get("status"))),
                ("Priority", named(fields.get("priority"))),
                (
                    "Labels",
                    " ".join(text(one) for one in labels) if isinstance(labels, list) else "",
                ),
                ("Updated", text(fields.get("updated"))),
            ]
        )
        records.append(
            Record(
                external_id=issue["key"],
                title=text(fields.get("summary"))[:200] or issue["key"],
                body=f"{state}\n\n{adf_text(fields.get('description'))}".strip(),
                url=f"{base}/browse/{quote(issue['key'])}",
            )
        )
    return records


def jira_search(base, headers, query):
    """Run a JQL search against whichever search endpoint this instance has.

    Jira Cloud removed `/rest/api/3/search` in 2025 and answers it with `410
    Gone`; Data Center still serves it and has no `/search/jql`. The current
    endpoint is tried first and the old one only when the instance says that URL
    is not there, so a real failure - a rejected credential, a malformed JQL - is
    reported once instead of being retried against a second URL and surfacing as
    the wrong error.

    Both return `{"issues": [...]}`, which is all the adapter below reads.
    """
    try:
        return api_json(f"{base}/rest/api/3/search/jql?{query}", headers, label="Jira")
    except ValidationError as failure:
        if getattr(failure, "http_status", None) not in {404, 410}:
            raise
    return api_json(f"{base}/rest/api/3/search?{query}", headers, label="Jira")


def adf_text(node, depth=0):
    """Flatten Atlassian Document Format to plain text.

    Jira Cloud returns rich text as nested JSON rather than a string. Depth is
    bounded because the structure comes from outside and nothing guarantees it is
    shallow.
    """
    if isinstance(node, str):
        return node
    if depth > 12 or not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return text(node.get("text"))
    children = node.get("content")
    if not isinstance(children, list):
        return ""
    joiner = "\n" if node.get("type") in {"doc", "paragraph", "listItem"} else ""
    return joiner.join(adf_text(child, depth + 1) for child in children)


# ----------------------------------------------------------------- ServiceNow

SERVICENOW_AUTH = [
    ("basic", "Basic (service account user + password)"),
    ("oauth", "OAuth client credentials"),
]


class ServiceNowForm(forms.Form):
    base_url = forms.CharField(
        max_length=200,
        label="Instance URL",
        help_text="For example https://yourinstance.service-now.com",
    )
    auth = forms.ChoiceField(choices=SERVICENOW_AUTH, label="Authentication", initial="basic")
    identity = forms.CharField(
        max_length=200,
        label="User name or client id",
        help_text="The secret half - password or client secret - is mounted as a file.",
    )
    table = forms.RegexField(
        regex=r"^[a-z0-9_]+$",
        max_length=80,
        initial="incident",
        label="Table",
        help_text="For example incident, problem, change_request, kb_knowledge.",
    )
    query = forms.CharField(
        max_length=400,
        required=False,
        label="Encoded query",
        help_text="Optional sysparm_query, for example active=true^priority<=2",
    )
    history_pages = forms.IntegerField(
        min_value=1,
        max_value=10,
        initial=1,
        required=False,
        label="Incident history pages",
        help_text=(
            "Import up to 10 pages of 100 incidents per sync. "
            "Use more pages for a bounded history backfill."
        ),
    )
    assignment_groups = forms.CharField(
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Incident assignment groups",
        help_text=(
            "For the incident table, enter one group name per line or separate names with commas. "
            "Only incidents assigned to these groups will be imported. Leave blank to keep "
            "the existing all-groups behavior."
        ),
    )
    title_field = forms.RegexField(
        regex=r"^[a-z0-9_]+$", max_length=80, initial="short_description", label="Title field"
    )
    body_field = forms.RegexField(
        regex=r"^[a-z0-9_]+$", max_length=80, initial="description", label="Body field"
    )

    def clean_base_url(self):
        return https_base(self.cleaned_data["base_url"])

    def clean_assignment_groups(self):
        value = self.cleaned_data["assignment_groups"]
        groups = parse_assignment_groups(value)
        if groups and self.cleaned_data.get("table") != "incident":
            raise forms.ValidationError("Assignment groups can only filter the incident table.")
        return "\n".join(groups)


def parse_assignment_groups(value):
    """Normalize group names before embedding them in a ServiceNow IN clause."""
    if not isinstance(value, str):
        raise forms.ValidationError("Enter group names as text.")
    groups = []
    seen = set()
    for raw in value.replace("\r", "\n").replace(",", "\n").splitlines():
        name = raw.strip()
        if not name:
            continue
        if len(name) > 100 or any(char in name for char in "^@=<>!"):
            raise forms.ValidationError(
                "Group names must be under 100 characters and cannot contain query operators."
            )
        key = name.casefold()
        if key not in seen:
            groups.append(name)
            seen.add(key)
        if len(groups) > 10:
            raise forms.ValidationError("Enter at most 10 assignment groups.")
    return groups


def servicenow_token(config, secret):
    """An access token for the OAuth client-credentials mode."""
    base = config["base_url"]
    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": config["identity"],
            "client_secret": secret,
        }
    ).encode()
    try:
        raw, _, _, _ = fetch(
            f"{base}/oauth_token.do",
            method="POST",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except FetchError as failure:
        raise ValidationError(f"ServiceNow sign-in: {failure}") from None
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    # A sign-in endpoint that answers with a list or a bare string is still a
    # refusal to authenticate. Reading it as a dictionary regardless turned that
    # into an AttributeError and a 500 instead of the message below.
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ValidationError("ServiceNow did not return an access token.")
    return token


def servicenow_records(config, secret):
    base = config["base_url"]
    if config.get("auth") == "oauth":
        headers = {"Authorization": f"Bearer {servicenow_token(config, secret)}"}
    else:
        headers = {"Authorization": basic(config["identity"], secret)}
    headers["Accept"] = "application/json"
    title_field = config.get("title_field") or "short_description"
    body_field = config.get("body_field") or "description"
    table = config["table"]
    groups = parse_assignment_groups(config.get("assignment_groups") or "")
    if groups and table != "incident":
        raise ValidationError("Assignment groups can only filter the incident table.")
    fields = ["sys_id", "number", title_field, body_field]
    if table == "incident":
        fields.extend(
            [
                "cmdb_ci",
                "business_service",
                "environment",
                "impact",
                "assignment_group",
                "priority",
                "state",
                "opened_at",
                "resolved_at",
                "close_code",
                "close_notes",
                "problem_id",
                "caused_by_change",
            ]
        )
    elif table == "change_request":
        fields.extend(["cmdb_ci", "business_service", "start_date", "end_date", "state", "type"])
    encoded_query = config.get("query") or "ORDERBYDESCsys_updated_on"
    if groups:
        encoded_query = f"assignment_group.nameIN{','.join(groups)}^{encoded_query}"
    try:
        pages = int(config.get("history_pages") or 1) if table == "incident" else 1
    except (TypeError, ValueError):
        raise ValidationError("Incident history pages must be between 1 and 10.") from None
    if not 1 <= pages <= 10:
        raise ValidationError("Incident history pages must be between 1 and 10.")
    rows = []
    seen_ids = set()
    for page in range(pages):
        query = urlencode(
            {
                "sysparm_limit": MAX_RECORDS,
                "sysparm_offset": page * MAX_RECORDS,
                "sysparm_query": encoded_query,
                "sysparm_fields": ",".join(dict.fromkeys(fields)),
                "sysparm_display_value": "true",
            }
        )
        payload = api_json(
            f"{base}/api/now/table/{quote(table)}?{query}", headers, label="ServiceNow"
        )
        batch = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(batch, list):
            raise ValidationError("ServiceNow returned an unexpected record list.")
        for row in batch:
            if not isinstance(row, dict) or not isinstance(row.get("sys_id"), str):
                raise ValidationError("ServiceNow returned an unexpected record.")
            if row["sys_id"] not in seen_ids:
                rows.append(row)
                seen_ids.add(row["sys_id"])
        if len(batch) < MAX_RECORDS:
            break
    records = []
    allowed_groups = {group.casefold() for group in groups}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("sys_id"), str):
            raise ValidationError("ServiceNow returned an unexpected record.")
        if allowed_groups:
            raw_group = row.get("assignment_group")
            group = (
                text(raw_group.get("display_value") or raw_group.get("name"))
                if isinstance(raw_group, dict)
                else text(raw_group)
            )
            if group.strip().casefold() not in allowed_groups:
                continue
        number = text(row.get("number")) or row["sys_id"]
        body = text(row.get(body_field))
        if table == "incident":
            state = incident_header(
                [
                    ("Type", "Incident"),
                    ("Number", number),
                    ("State", named(row.get("state"))),
                    ("Priority", named(row.get("priority"))),
                    ("Impact", named(row.get("impact"))),
                    ("Service", named(row.get("business_service"))),
                    ("CI", named(row.get("cmdb_ci"))),
                    ("Environment", named(row.get("environment"))),
                    ("Assignment group", named(row.get("assignment_group"))),
                    ("Opened", text(row.get("opened_at"))),
                    ("Resolved", text(row.get("resolved_at"))),
                    ("Close code", named(row.get("close_code"))),
                    ("Close notes", text(row.get("close_notes"))),
                    ("Problem", named(row.get("problem_id"))),
                    ("Caused by change", named(row.get("caused_by_change"))),
                ]
            )
            body = f"{state}\n\n{body}".strip()
        elif table == "change_request":
            state = incident_header(
                [
                    ("Type", "Change"),
                    ("Number", number),
                    ("State", named(row.get("state"))),
                    ("Service", named(row.get("business_service"))),
                    ("CI", named(row.get("cmdb_ci"))),
                    ("Start", text(row.get("start_date"))),
                    ("End", text(row.get("end_date"))),
                    ("Change type", named(row.get("type"))),
                ]
            )
            body = f"{state}\n\n{body}".strip()
        records.append(
            Record(
                external_id=row["sys_id"],
                title=(text(row.get(title_field))[:200] or number),
                body=body,
                url=f"{base}/nav_to.do?uri={quote(table)}.do%3Fsys_id%3D{quote(row['sys_id'])}",
            )
        )
    return records


# ------------------------------------------------------------------ registry


def github_namespace(config):
    return f"https://github.com/{config['repository']}/issues/"


def jira_namespace(config):
    return f"{config['base_url']}/browse/"


def servicenow_namespace(config):
    return f"{config['base_url']}/nav_to.do?uri={quote(config['table'])}.do"


@dataclass(frozen=True)
class Kind:
    key: str
    #: The system's name, as it is written. Not "GitHub issues" - what is imported
    #: is described in `summary`, and repeating it in the name reads as clutter
    #: once the list has a column for the target anyway.
    label: str
    icon: str
    #: One line describing what an import brings in, shown on the chooser card.
    summary: str
    form: type
    records: callable
    #: Which config field identifies the target, for display and for uniqueness.
    identity_field: str
    #: True when the mounted credential is required rather than optional.
    credential_required: bool
    #: The URL prefix every record from this connector shares. Pruning needs to
    #: know which knowledge is this connector's own, and a record's source URL is
    #: the only thing tying the two together - KnowledgeEntry deliberately has no
    #: connector, because knowledge outlives the connector that brought it in.
    namespace: callable

    def identity(self, config):
        return (config or {}).get(self.identity_field, "")


KINDS = {
    kind.key: kind
    for kind in (
        Kind(
            "github",
            "GitHub",
            "github",
            "Issues from a repository. A mounted token raises the rate limit and "
            "reaches private repositories.",
            GitHubForm,
            github_records,
            "repository",
            False,
            github_namespace,
        ),
        Kind(
            "jira",
            "Jira",
            "jira",
            "Issues from a Cloud site or a Data Center instance, optionally narrowed by JQL.",
            JiraForm,
            jira_records,
            "base_url",
            True,
            jira_namespace,
        ),
        Kind(
            "servicenow",
            "ServiceNow",
            "servicenow",
            "Records from any table - incidents, problems, changes or knowledge articles.",
            ServiceNowForm,
            servicenow_records,
            "base_url",
            True,
            servicenow_namespace,
        ),
    )
}


def kind_for(key):
    try:
        return KINDS[key]
    except KeyError:
        raise ValidationError("Unsupported connector type.") from None
