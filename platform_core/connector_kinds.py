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
        raise ValidationError(f"{label}: {failure}") from None
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
        records.append(
            Record(
                external_id=str(item["number"]),
                title=item["title"][:200],
                body=text(item.get("body")),
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
        help_text="Jira Cloud only. The API token itself is mounted as a file, never entered here.",
    )
    jql = forms.CharField(
        max_length=400,
        required=False,
        label="JQL filter",
        help_text='Optional, for example project = OPS AND updated >= -30d',
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
            "fields": "summary,description,updated",
        }
    )
    payload = api_json(f"{base}/rest/api/3/search?{query}", headers, label="Jira")
    issues = payload.get("issues") if isinstance(payload, dict) else None
    if not isinstance(issues, list):
        raise ValidationError("Jira returned an unexpected issue list.")
    records = []
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("key"), str):
            raise ValidationError("Jira returned an unexpected issue record.")
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        records.append(
            Record(
                external_id=issue["key"],
                title=text(fields.get("summary"))[:200] or issue["key"],
                body=adf_text(fields.get("description")),
                url=f"{base}/browse/{quote(issue['key'])}",
            )
        )
    return records


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
    title_field = forms.RegexField(
        regex=r"^[a-z0-9_]+$", max_length=80, initial="short_description", label="Title field"
    )
    body_field = forms.RegexField(
        regex=r"^[a-z0-9_]+$", max_length=80, initial="description", label="Body field"
    )

    def clean_base_url(self):
        return https_base(self.cleaned_data["base_url"])


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
    query = urlencode(
        {
            "sysparm_limit": MAX_RECORDS,
            "sysparm_query": config.get("query") or "ORDERBYDESCsys_updated_on",
            "sysparm_fields": f"sys_id,number,{title_field},{body_field}",
            "sysparm_display_value": "true",
        }
    )
    table = config["table"]
    payload = api_json(
        f"{base}/api/now/table/{quote(table)}?{query}", headers, label="ServiceNow"
    )
    rows = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValidationError("ServiceNow returned an unexpected record list.")
    records = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("sys_id"), str):
            raise ValidationError("ServiceNow returned an unexpected record.")
        number = text(row.get("number")) or row["sys_id"]
        records.append(
            Record(
                external_id=row["sys_id"],
                title=(text(row.get(title_field))[:200] or number),
                body=text(row.get(body_field)),
                url=f"{base}/nav_to.do?uri={quote(table)}.do%3Fsys_id%3D{quote(row['sys_id'])}",
            )
        )
    return records


# ------------------------------------------------------------------ registry


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
        ),
    )
}


def kind_for(key):
    try:
        return KINDS[key]
    except KeyError:
        raise ValidationError("Unsupported connector type.") from None
