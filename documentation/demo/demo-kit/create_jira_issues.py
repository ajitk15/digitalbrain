"""Create the seven CARE tickets in a real Jira Cloud project.

The board in this kit is a fixture: `jira-tickets.md` for reading and
`jira-export.json` for the import shape. This script puts the same seven items
into a live Jira site, so the demonstration can import them through the
platform's own Jira connector rather than through a stand-in.

    python create_jira_issues.py --site https://you.atlassian.net \
        --email you@example.com --project KAN \
        --token-file ../../.runtime/secrets/jira_<application id> --dry-run

Three things worth knowing before running it:

* Jira assigns issue keys itself, so a ticket lands as `<PROJECT>-1`, never as
  `CARE-401`. The demo identifier is kept in two places that do survive: the
  summary is prefixed `CARE-401 - ...`, and the label `care-401` is applied.
  Every document in `docs/` refers to tickets by those identifiers.
* The token is read from a file, never from the command line, so it stays out of
  the shell history and the process list. Same rule as the platform's secrets.
* Re-running is safe. An issue already carrying its `care-NNN` label in that
  project is skipped rather than duplicated.

Bodies come from `build_jira_export.py`, so the fixture and the live board cannot
drift apart; labels come from `CARE-board-export.csv`.
"""

import argparse
import base64
import csv
import importlib.util
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT = 30


def load_issues():
    """The seven tickets: demo key, summary, type, priority, body, labels."""
    spec = importlib.util.spec_from_file_location("_export", HERE / "build_jira_export.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    labels = {}
    with (HERE / "CARE-board-export.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            labels[row["Issue key"]] = row["Labels"].split()
    issues = []
    for key, summary, kind, priority, _updated, body in module.ISSUES:
        issues.append(
            {
                "care_key": key,
                "summary": f"{key} - {summary}",
                "type": kind,
                "priority": priority,
                "body": body,
                "labels": [key.lower()] + labels.get(key, []),
            }
        )
    return issues, module.adf


class Jira:
    """The smallest Jira Cloud REST client this script needs."""

    def __init__(self, site, email, token):
        self.site = site.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def call(self, path, payload=None, method=None):
        request = urllib.request.Request(
            self.site + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method or ("POST" if payload is not None else "GET"),
            headers={
                "Authorization": self.auth,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as failure:
            detail = failure.read().decode("utf-8", "replace")[:600]
            raise SystemExit(f"Jira {failure.code} on {method or 'GET'} {path}: {detail}") from None
        except urllib.error.URLError as failure:
            raise SystemExit(f"Jira unreachable: {failure.reason}") from None
        return json.loads(raw) if raw else {}


def issue_types(jira, project):
    """Map the kit's type names onto the ones this project actually offers."""
    # Jira Cloud answers this endpoint with "issueTypes"; the paginated shape in
    # the documentation uses "values". Accept both, then fall back to the
    # deprecated whole-createmeta call for a Data Center instance that has
    # neither. Reading only one of the three reported a project with no types at
    # all, which is never true of a project that exists.
    payload = jira.call(f"/rest/api/3/issue/createmeta/{project}/issuetypes")
    offered = payload.get("issueTypes") or payload.get("values") or []
    if not offered:
        payload = jira.call(f"/rest/api/3/issue/createmeta?projectKeys={project}")
        projects = payload.get("projects") or []
        offered = projects[0].get("issuetypes", []) if projects else []
    available = {
        entry["name"].lower(): entry["id"] for entry in offered if not entry.get("subtask")
    }
    if not available:
        raise SystemExit(f"Project {project} offers no issue type this token can create.")
    return available


def resolve(kind, available):
    """This project's id for one type, falling back to Task, then to anything."""
    for candidate in (kind.lower(), "task", "story"):
        if candidate in available:
            return available[candidate], candidate != kind.lower()
    name = next(iter(available))
    return available[name], True


def existing(jira, project, label):
    """The key of an issue already carrying this label, or "" when there is none.

    Jira Cloud removed /rest/api/3/search in 2025 and answers it with 410 Gone.
    The current endpoint is tried first; Data Center, which has only the old one,
    falls back to it.
    """
    jql = urllib.parse.quote(f'project = "{project}" AND labels = "{label}"')
    query = f"jql={jql}&fields=key&maxResults=1"
    try:
        found = jira.call(f"/rest/api/3/search/jql?{query}")
    except SystemExit as failure:
        if " 404 " not in str(failure) and " 410 " not in str(failure):
            raise
        found = jira.call(f"/rest/api/3/search?{query}")
    hits = found.get("issues") or []
    return hits[0]["key"] if hits else ""


def create(jira, project, issue, type_id, adf, with_priority):
    fields = {
        "project": {"key": project},
        "summary": issue["summary"][:250],
        "description": adf(issue["body"]),
        "issuetype": {"id": type_id},
        "labels": issue["labels"],
    }
    if with_priority:
        fields["priority"] = {"name": issue["priority"]}
    return jira.call("/rest/api/3/issue", {"fields": fields})


def main():
    parser = argparse.ArgumentParser(description="Create the CARE board in a live Jira project.")
    parser.add_argument("--site", required=True, help="https://your-team.atlassian.net")
    parser.add_argument("--email", required=True, help="the Atlassian account the token belongs to")
    parser.add_argument("--project", required=True, help="project key, for example KAN")
    parser.add_argument("--token-file", help="file holding the API token and nothing else")
    parser.add_argument("--no-priority", action="store_true", help="omit the priority field")
    parser.add_argument("--dry-run", action="store_true", help="print what would be created")
    arguments = parser.parse_args()

    issues, adf = load_issues()
    if arguments.dry_run:
        for issue in issues:
            print(f"{issue['type']:<6} {issue['priority']:<8} {issue['summary']}")
            print(f"       labels: {' '.join(issue['labels'])}")
        print(f"\n{len(issues)} issues would be created in {arguments.project}.")
        return

    if not arguments.token_file:
        raise SystemExit("--token-file is required unless --dry-run is given.")
    token = Path(arguments.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"{arguments.token_file} is empty.")
    jira = Jira(arguments.site, arguments.email, token)
    who = jira.call("/rest/api/3/myself")
    print(f"Authenticated as {who.get('displayName')} <{who.get('emailAddress')}>")

    available = issue_types(jira, arguments.project)
    print(f"Project {arguments.project} offers: {', '.join(sorted(available))}")

    created = {}
    for issue in issues:
        already = existing(jira, arguments.project, issue["care_key"].lower())
        if already:
            print(f"  = {issue['care_key']} already present as {already}")
            created[issue["care_key"]] = already
            continue
        type_id, substituted = resolve(issue["type"], available)
        try:
            result = create(jira, arguments.project, issue, type_id, adf, not arguments.no_priority)
        except SystemExit as failure:
            if "priority" not in str(failure) or arguments.no_priority:
                raise
            print(f"  ! {issue['care_key']}: priority is not on the create screen, omitting it")
            result = create(jira, arguments.project, issue, type_id, adf, False)
        note = f" (as {issue['type']} is unavailable)" if substituted else ""
        print(f"  + {issue['care_key']} -> {result['key']}{note}")
        created[issue["care_key"]] = result["key"]

    target = HERE / "jira-created.json"
    target.write_text(
        json.dumps({"site": jira.site, "project": arguments.project, "issues": created}, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {target}")
    print(f"Connector JQL: project = {arguments.project} ORDER BY updated DESC")


if __name__ == "__main__":
    sys.exit(main())
