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
    if not issues:
        jira_identity(base, headers)
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


def jira_identity(base, headers):
    """Raise if Jira does not accept this credential.

    Jira Cloud does not refuse a search made with a revoked or expired API
    token: it runs it anonymously and answers 200 with no issues. An import then
    reported success with nothing in it, and the board looked empty rather than
    disconnected. So an empty search is followed by asking who the credential
    belongs to - `/rest/api/2/myself`, which Cloud and Data Center both serve and
    which does answer 401 to an anonymous caller. A search that returned issues
    has already proved the credential, and costs no second request.
    """
    try:
        api_json(f"{base}/rest/api/2/myself", headers, label="Jira")
    except ValidationError as failure:
        if getattr(failure, "http_status", None) in {401, 403}:
            raise ValidationError(
                "Jira rejected the credential, so the search ran anonymously and found "
                "nothing. The API token may have expired or been revoked: create a new one "
                "and save it on the Credentials screen."
            ) from None
        raise


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
    # A knowledge article's body is `text`, and a flow is named by `name`; the
    # form's defaults are the incident table's. Only the defaults are replaced -
    # a field someone chose on purpose stands.
    if table == "kb_knowledge" and body_field == "description":
        body_field = "text"
    if table == "sys_hub_flow" and title_field == "short_description":
        title_field = "name"
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
    elif table == "kb_knowledge":
        # Every workflow state is imported, retired ones included: the graph
        # reads the state and keeps only what ServiceNow calls Published and
        # in date, so an article retired there leaves the graph on the next sync.
        fields.extend(
            [
                "kb_category",
                "kb_knowledge_base",
                "workflow_state",
                "valid_to",
                "cmdb_ci",
                "meta",
                "sys_updated_on",
            ]
        )
    elif table == "sys_hub_flow":
        fields.extend(["name", "description", "active", "status", "sys_updated_on"])
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
        elif table == "kb_knowledge":
            state = incident_header(
                [
                    ("Type", "Knowledge article"),
                    ("Number", number),
                    ("State", named(row.get("workflow_state"))),
                    ("Category", named(row.get("kb_category"))),
                    ("Knowledge base", named(row.get("kb_knowledge_base"))),
                    ("CI", named(row.get("cmdb_ci"))),
                    ("Valid to", text(row.get("valid_to"))),
                    ("Keywords", text(row.get("meta"))),
                    ("Updated", text(row.get("sys_updated_on"))),
                ]
            )
            body = f"{state}\n\n{html_text(body)}".strip()
        elif table == "sys_hub_flow":
            description = text(row.get("description"))
            active = text(row.get("active")).casefold()
            records.append(
                automation_record(
                    "ServiceNow Flow Designer",
                    row["sys_id"],
                    text(row.get(title_field)) or number,
                    description,
                    f"{base}/nav_to.do?uri={quote(table)}.do%3Fsys_id%3D{quote(row['sys_id'])}",
                    endpoint=base,
                    meta=automation_meta((), description),
                    state="Inactive" if active in {"false", "0"} else named(row.get("status")),
                    last=(text(row.get("sys_updated_on")), ""),
                )
            )
            continue
        records.append(
            Record(
                external_id=row["sys_id"],
                title=(text(row.get(title_field))[:200] or number),
                body=body,
                url=f"{base}/nav_to.do?uri={quote(table)}.do%3Fsys_id%3D{quote(row['sys_id'])}",
            )
        )
    return records


# ---------------------------------------------------------------- Automations
#
# An automation is a remediation someone can run - an AWX job template, a
# Rundeck job, an Azure Automation runbook, a ServiceNow flow, or a line in a
# list a team maintains. Every source is reduced to the same header, so the
# operations graph links them all the same way: what they target (CI, service)
# and what they remediate (symptoms). This platform never runs one. Triage may
# suggest an automation and a person may *simulate* a run, which records what
# would have been sent and sends nothing (`serviceops.simulate_automation`).

#: Metadata keys an automation source may carry in its tags, labels, columns or
#: "Key: value" description lines, and the header field each one becomes.
AUTOMATION_META = {
    "ci": "CI",
    "component": "CI",
    "service": "Service",
    "symptom": "Symptoms",
    "symptoms": "Symptoms",
    "risk": "Risk",
    "approval": "Approval",
    "inputs": "Inputs",
}


def automation_meta(tags=(), description=""):
    """Header fields from `key:value` tags and `Key: value` description lines.

    Tags win over the description, and a repeated symptom accumulates. Values
    are kept on one line so nothing a source says can forge another field.
    """
    meta = {}

    def put(key, value):
        field = AUTOMATION_META.get(key.strip().casefold())
        value = " ".join(str(value).split())[:300]
        if not field or not value:
            return
        if field == "Symptoms" and meta.get(field):
            meta[field] = f"{meta[field]}, {value}"
        else:
            meta.setdefault(field, value)

    for tag in tags:
        if isinstance(tag, tuple):
            put(*tag)
        elif isinstance(tag, str) and ":" in tag:
            put(*tag.split(":", 1))
    for line in (description or "").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            put(key, value)
    return meta


def automation_record(
    platform, external_id, name, description, url, *, endpoint, meta, state="", last=("", "")
):
    """One automation, as every source reduces it."""
    fields = [
        ("Type", "Automation"),
        ("Number", f"{platform} {external_id}"[:120]),
        ("State", state),
        ("Platform", platform),
        ("Automation ID", str(external_id)),
        ("Endpoint", endpoint),
        ("CI", meta.get("CI", "")),
        ("Service", meta.get("Service", "")),
        ("Symptoms", meta.get("Symptoms", "")),
        ("Risk", meta.get("Risk", "")),
        ("Approval", meta.get("Approval", "")),
        ("Inputs", meta.get("Inputs", "")),
        ("Last run", last[0]),
        ("Last status", last[1]),
    ]
    return Record(
        external_id=str(external_id),
        title=(name or str(external_id))[:200],
        body=f"{incident_header(fields)}\n\n{description or ''}".strip(),
        url=url,
    )


class AWXForm(forms.Form):
    base_url = forms.CharField(
        max_length=200,
        label="AWX or Automation Controller URL",
        help_text="For example https://awx.example.com. The API token belongs on the "
        "Credentials screen.",
    )

    def clean_base_url(self):
        return https_base(self.cleaned_data["base_url"])


def awx_records(config, secret):
    base = config["base_url"]
    headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json"}
    payload = api_json(
        f"{base}/api/v2/job_templates/?page_size={MAX_RECORDS}&order_by=-modified",
        headers,
        label="AWX",
    )
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValidationError("AWX returned an unexpected job template list.")
    records = []
    for item in items:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            raise ValidationError("AWX returned an unexpected job template.")
        summary = item.get("summary_fields") if isinstance(item.get("summary_fields"), dict) else {}
        labels = (summary.get("labels") or {}).get("results") if isinstance(
            summary.get("labels"), dict
        ) else []
        last = summary.get("last_job") if isinstance(summary.get("last_job"), dict) else {}
        inputs = [
            label
            for flag, label in (
                ("ask_variables_on_launch", "extra variables"),
                ("survey_enabled", "survey"),
                ("ask_limit_on_launch", "host limit"),
            )
            if item.get(flag) is True
        ]
        meta = automation_meta(
            [named(one) for one in labels or [] if isinstance(one, dict)],
            text(item.get("description")),
        )
        meta.setdefault("Inputs", ", ".join(inputs))
        records.append(
            automation_record(
                "AWX",
                item["id"],
                text(item.get("name")),
                "\n".join(
                    part
                    for part in (
                        text(item.get("description")),
                        f"Playbook: {text(item.get('playbook'))}" if item.get("playbook") else "",
                    )
                    if part
                ),
                f"{base}/#/templates/job_template/{item['id']}/details",
                endpoint=base,
                meta=meta,
                last=(text(last.get("finished")), text(last.get("status"))),
            )
        )
    return records


class RundeckForm(forms.Form):
    base_url = forms.CharField(
        max_length=200,
        label="Rundeck URL",
        help_text="For example https://rundeck.example.com. The API token belongs on the "
        "Credentials screen.",
    )
    project = forms.RegexField(regex=r"^[A-Za-z0-9_.-]{1,100}$", label="Project")

    def clean_base_url(self):
        return https_base(self.cleaned_data["base_url"])


def rundeck_records(config, secret):
    base, project = config["base_url"], config["project"]
    headers = {"X-Rundeck-Auth-Token": secret, "Accept": "application/json"}
    items = api_json(f"{base}/api/41/project/{quote(project)}/jobs", headers, label="Rundeck")
    if not isinstance(items, list):
        raise ValidationError("Rundeck returned an unexpected job list.")
    records = []
    for item in items[:MAX_RECORDS]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValidationError("Rundeck returned an unexpected job.")
        group = text(item.get("group"))
        records.append(
            automation_record(
                "Rundeck",
                item["id"],
                (f"{group}/" if group else "") + text(item.get("name")),
                text(item.get("description")),
                f"{base}/project/{quote(project)}/job/show/{quote(item['id'])}",
                endpoint=base,
                meta=automation_meta((), text(item.get("description"))),
                state="Disabled" if item.get("enabled") is False else "",
            )
        )
    return records


class AzureAutomationForm(forms.Form):
    tenant = forms.RegexField(regex=r"^[A-Za-z0-9.-]{1,100}$", label="Tenant (directory) id")
    client_id = forms.RegexField(regex=r"^[A-Za-z0-9-]{1,100}$", label="App registration client id")
    subscription = forms.RegexField(regex=r"^[A-Za-z0-9-]{1,100}$", label="Subscription id")
    resource_group = forms.RegexField(regex=r"^[\w.()-]{1,90}$", label="Resource group")
    account = forms.RegexField(regex=r"^[A-Za-z0-9-]{1,50}$", label="Automation account")


def azure_token(config, secret):
    """A management-plane token for the app registration (client credentials)."""
    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": config["client_id"],
            "client_secret": secret,
            "scope": "https://management.azure.com/.default",
        }
    ).encode()
    try:
        raw, _, _, _ = fetch(
            f"https://login.microsoftonline.com/{quote(config['tenant'])}/oauth2/v2.0/token",
            method="POST",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except FetchError as failure:
        raise ValidationError(f"Azure sign-in: {failure}") from None
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ValidationError("Azure did not return an access token.")
    return token


def azure_account_path(config):
    return (
        f"subscriptions/{quote(config['subscription'])}/resourceGroups/"
        f"{quote(config['resource_group'])}/providers/Microsoft.Automation/"
        f"automationAccounts/{quote(config['account'])}"
    )


def azure_automation_records(config, secret):
    token = azure_token(config, secret)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    path = azure_account_path(config)
    payload = api_json(
        f"https://management.azure.com/{path}/runbooks?api-version=2023-11-01",
        headers,
        label="Azure Automation",
    )
    items = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValidationError("Azure Automation returned an unexpected runbook list.")
    portal = f"https://portal.azure.com/#@{quote(config['tenant'])}/resource/{path}/runbooks/"
    records = []
    for item in items[:MAX_RECORDS]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValidationError("Azure Automation returned an unexpected runbook.")
        props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        tags = item.get("tags") if isinstance(item.get("tags"), dict) else {}
        records.append(
            automation_record(
                "Azure Automation",
                item["name"],
                item["name"],
                text(props.get("description")),
                portal + quote(item["name"]),
                endpoint=f"https://management.azure.com/{path}",
                meta=automation_meta(
                    [(key, value) for key, value in tags.items() if isinstance(value, str)],
                    text(props.get("description")),
                ),
                state=text(props.get("state")),
                last=(text(props.get("lastModifiedTime")), ""),
            )
        )
    return records


class AutomationListForm(forms.Form):
    """A list a team maintains: pasted here, or fetched from an https URL."""

    list_name = forms.RegexField(
        regex=r"^[A-Za-z0-9 _.-]{1,80}$",
        label="List name",
        help_text="How this list is named in the graph, for example Ops runbook automations.",
    )
    source_url = forms.CharField(
        max_length=300,
        required=False,
        label="CSV address",
        help_text="An https address of a CSV file, for example a raw file in Git. A token on "
        "the Credentials screen is sent as a bearer token. Leave empty to paste the list below.",
    )
    content = forms.CharField(
        max_length=50_000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 8}),
        label="Or paste the CSV",
        help_text="Columns: name (required), id, platform, endpoint, ci, service, symptoms, "
        "risk, approval, inputs, description, url.",
    )

    def clean_source_url(self):
        value = self.cleaned_data["source_url"].strip()
        return https_base(value) if value else ""

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("source_url") and not (cleaned.get("content") or "").strip():
            raise forms.ValidationError("Give a CSV address or paste the list.")
        return cleaned


def automation_list_namespace(config):
    return f"automation-list:{config['list_name']}/"


def automation_list_records(config, secret):
    import csv
    import io
    import re

    if config.get("source_url"):
        headers = {"Accept": "text/csv, text/plain"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        try:
            raw, _, _, _ = fetch(config["source_url"], headers=headers)
        except FetchError as failure:
            raise ValidationError(f"Automation list: {failure}") from None
        content = raw.decode("utf-8-sig", errors="replace")
    else:
        content = config.get("content") or ""
    reader = csv.DictReader(io.StringIO(content))
    columns = {name.strip().casefold() for name in reader.fieldnames or []}
    if "name" not in columns:
        raise ValidationError("The automation list needs a header row with a name column.")
    namespace = automation_list_namespace(config)
    records, seen = [], set()
    for row in reader:
        row = {(key or "").strip().casefold(): (value or "").strip() for key, value in row.items()}
        name = row.get("name", "")
        if not name:
            continue
        identifier = row.get("id") or re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        link = row.get("url", "")
        records.append(
            automation_record(
                row.get("platform") or "Automation list",
                identifier,
                name,
                row.get("description", ""),
                link if link.startswith("https://") else namespace + quote(identifier),
                endpoint=row["endpoint"] if row.get("endpoint", "").startswith("https://") else "",
                meta=automation_meta([(key, row.get(key, "")) for key in AUTOMATION_META]),
            )
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


def html_text(value):
    """Readable text from a knowledge article's HTML: tags out, entities decoded."""
    import html
    import re

    value = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", value or "")
    value = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", value)
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    lines = (" ".join(line.split()) for line in value.splitlines())
    return "\n".join(line for line in lines if line)


# ------------------------------------------------------------------ registry


def github_namespace(config):
    return f"https://github.com/{config['repository']}/issues/"


def jira_namespace(config):
    return f"{config['base_url']}/browse/"


def servicenow_namespace(config):
    return f"{config['base_url']}/nav_to.do?uri={quote(config['table'])}.do"


def awx_namespace(config):
    return f"{config['base_url']}/#/templates/job_template/"


def rundeck_namespace(config):
    return f"{config['base_url']}/project/{quote(config['project'])}/job/show/"


def azure_automation_namespace(config):
    tenant = quote(config["tenant"])
    return f"https://portal.azure.com/#@{tenant}/resource/{azure_account_path(config)}/runbooks/"


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
            "Records from any table - incidents, problems, changes, knowledge articles "
            "(kb_knowledge) or Flow Designer flows (sys_hub_flow).",
            ServiceNowForm,
            servicenow_records,
            "base_url",
            True,
            servicenow_namespace,
        ),
        Kind(
            "awx",
            "Ansible AWX",
            "settings",
            "Job templates from AWX or Automation Controller, with their labels and last run. "
            "Listed as automations; never launched from here.",
            AWXForm,
            awx_records,
            "base_url",
            True,
            awx_namespace,
        ),
        Kind(
            "rundeck",
            "Rundeck",
            "refresh",
            "Jobs from one Rundeck project. Listed as automations; never run from here.",
            RundeckForm,
            rundeck_records,
            "project",
            True,
            rundeck_namespace,
        ),
        Kind(
            "azure_automation",
            "Azure Automation",
            "network",
            "Runbooks from one Automation account, with their tags. Listed as automations; "
            "never started from here.",
            AzureAutomationForm,
            azure_automation_records,
            "account",
            True,
            azure_automation_namespace,
        ),
        Kind(
            "automation_list",
            "Automation list",
            "document",
            "A CSV of automations your team maintains, pasted here or fetched from an "
            "https address.",
            AutomationListForm,
            automation_list_records,
            "list_name",
            False,
            automation_list_namespace,
        ),
    )
}


def kind_for(key):
    try:
        return KINDS[key]
    except KeyError:
        raise ValidationError("Unsupported connector type.") from None
