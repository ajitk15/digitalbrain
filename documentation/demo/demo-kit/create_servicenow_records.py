"""Create the labelled CarePath incidents and changes in a real ServiceNow instance.

`benchmarks/triage-pack.json` holds six incidents and eight changes - five open
incidents, each with the change or resolved incident that caused it, and decoy
changes beside them. `manage.py triage_pack --load-incidents` adds them straight
to an application; this script puts the same records into a live ServiceNow
instance instead, so the platform imports them through its own ServiceNow
connector, exactly as a customer's records arrive.

    python create_servicenow_records.py --instance https://dev401026.service-now.com \
        --user digibrain --password-file ../../.runtime/secrets/servicenow_<app id> --dry-run

What to know before running it:

* **It writes to ServiceNow.** Fourteen records (with `--create-missing`, the three
  configuration items, the business service and the assignment group too, where the
  instance lacks them - a fresh developer instance has none of them). Run it with
  `--dry-run` first; it prints every record and touches nothing. The account needs
  to create incidents and changes - the read-only account the connector uses
  (`itil` plus `snc_read_only`) cannot.
* **ServiceNow numbers the records itself**, so INC0011201 in the pack lands as
  whatever number is next. The pack's id is kept in `correlation_id`, which is
  also how a second run knows a record exists and skips it.
* **The password is read from a file**, never from the command line, so it stays
  out of the shell history and the process list.
* **References are looked up, dates are sent as UTC.** The CI, business service
  and assignment group are found by name and sent as their sys_id; a missing one
  stops the run before anything is written, unless `--create-missing` may add it -
  a CI in a class (`--ci-class`, Application by default), since the base `cmdb_ci` table
  refuses inserts without a CMDB role.
  Dates use ServiceNow's internal UTC format, so the account's time zone cannot
  move a change out of the two days before its incident, which is what links them.
* **The connectors must see them.** The demo's incident connector keeps assignment
  group CAREOPS and its change connector business service CAREPATH_OPS; both are
  the defaults here. The pack's `carepath-api-blue` is written as
  `carepath-api-green`, the CI the demo instance already has (`--api-ci`).
* **Knowledge articles too, with `--knowledge-base <title>`**: two published
  articles from the operations runbook, on the same CIs, which the connector's
  `kb_knowledge` import brings in and the operations graph links to the incidents.
  Publishing through the API needs a knowledge manager or admin; a third
  ServiceNow connector on table `kb_knowledge` imports them.
* Labels are not created in ServiceNow. Triage is scored against them by
  `triage_pack --labelled` only for records loaded from the pack; for records
  imported from here, confirm each cause on the incident's page in ServiceOps.

Standard library only, like `create_jira_issues.py`.
"""

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parents[2] / "benchmarks" / "triage-pack.json"
TIMEOUT = 30
#: Shown in each record's correlation display, so a person can see where it came from.
MARK = "Digital Brain demo"
#: ServiceNow's default priority matrix: (impact, urgency) for each priority.
PRIORITY = {"1": ("1", "1"), "2": ("1", "2"), "3": ("2", "2"), "4": ("2", "3"), "5": ("3", "3")}
#: Incident state values in a stock instance.
STATES = {"New": "1", "In Progress": "2", "On Hold": "3", "Resolved": "6", "Closed": "7"}
#: Knowledge articles, created with `--knowledge-base`: published and in date, on
#: the CIs the incidents are on, so the operations graph links each to them ("covers"
#: by CI). Written from the CarePath operations runbook (OPS-CAREPATH-002).
ARTICLES = (
    {
        "title": "Readiness is failing while liveness still answers",
        "ci": "carepath-data",
        "keywords": "readiness 503 health ready database disk full volume",
        "text": (
            "<p>If /health/live still answers, the process is fine and the database is "
            "not: do not restart the instances.</p><ol><li>Check the carepath-data volume is "
            "mounted and not full - snapshots kept on the same volume fill it.</li><li>Check "
            "the slow-query log for a transaction held open past the 2s ceiling (NFR-11)."
            "</li><li>If the database is unrecoverable, fail over to the standby deployment "
            "against the previous snapshot and raise Sev-1.</li></ol>"
        ),
    },
    {
        "title": "Rotating a partner API token",
        "ci": "carepath-api-blue",
        "keywords": "token rotation 401 partner export digest ADR-0002",
        "text": (
            "<p>Rotation is every 90 days (ADR-0002), in two deployments, deliberately."
            "</p><ol><li>Generate the new secret out of band.</li><li>Add its SHA-256 digest "
            "to the directory and deploy.</li><li>Give the holder the new token.</li><li>"
            "Remove the old digest and deploy again.</li></ol><p>One deployment that swaps "
            "both at once leaves a window in which the holder's old token is refused with "
            "401 and they do not yet have the new one.</p>"
        ),
    },
)


class ServiceNow:
    """The smallest Table API client this script needs."""

    def __init__(self, instance, user, password):
        self.instance = instance.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, table, query=None, payload=None):
        url = f"{self.instance}/api/now/table/{urllib.parse.quote(table)}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            method="POST" if payload is not None else "GET",
            headers={
                "Authorization": self.auth,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode() or "{}").get("result")
        except urllib.error.HTTPError as failure:
            detail = failure.read().decode(errors="replace")[:300]
            raise SystemExit(f"ServiceNow refused {table}: HTTP {failure.code} {detail}") from None
        except urllib.error.URLError as failure:
            raise SystemExit(f"ServiceNow could not be reached: {failure.reason}") from None

    def find(self, table, query):
        """The sys_id of the first row matching `query`, or None."""
        rows = self.call(table, {"sysparm_query": query, "sysparm_limit": 1,
                                 "sysparm_fields": "sys_id"})
        return rows[0]["sys_id"] if rows else None


def utc(value):
    """An ISO date from the pack as ServiceNow's internal UTC format."""
    if not value:
        return ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def load(api_ci):
    """The pack's records, with the API's CI renamed to the instance's."""
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    records = pack["records"]
    for record in records:
        # The name is in the text too (a close note names what was restarted), so
        # it is renamed everywhere, or the record would disagree with its own CI.
        for key in ("title", "text"):
            record[key] = record[key].replace("carepath-api-blue", api_ci)
        for key, value in record["fields"].items():
            record["fields"][key] = value.replace("carepath-api-blue", api_ci)
    return records


def payload(record, ids, close_code):
    """The Table API body for one pack record; references as sys_ids."""
    fields = record["fields"]
    body = {
        "short_description": record["title"],
        "description": record["text"],
        "cmdb_ci": ids["ci"][fields["CI"]],
        "business_service": ids["service"],
        "correlation_id": record["sys_id"],
        "correlation_display": MARK,
    }
    if record["table"] == "incident":
        impact, urgency = PRIORITY[fields.get("Priority", "3")[:1]]
        body.update(
            assignment_group=ids["group"],
            impact=impact,
            urgency=urgency,
            opened_at=utc(fields.get("Opened")),
            state=STATES.get(fields.get("State", "New"), "1"),
        )
        if fields.get("State") == "Resolved":
            body.update(
                resolved_at=utc(fields.get("Resolved")),
                close_code=close_code,
                close_notes=fields.get("Close notes", ""),
            )
    else:
        body.update(
            start_date=utc(fields.get("Start")),
            end_date=utc(fields.get("End")),
            type=fields.get("Change type", "Normal").lower(),
        )
    return body


def references(now, records, args):
    """{"ci": {name: sys_id}, "service": sys_id, "group": sys_id}, found by name.

    Stops before any ticket is written when one is missing, unless
    `--create-missing` lets it add it."""
    ids = {"ci": {}}
    for name in sorted({record["fields"]["CI"] for record in records}):
        found = now.find("cmdb_ci", f"name={name}")
        if found is None and args.create_missing and not args.dry_run:
            # Into a class, never the base table: the base `cmdb_ci` refuses an insert
            # from anyone without a CMDB role, and a CI belongs to a class anyway. It is
            # still found through `cmdb_ci`, which reads every class.
            found = now.call(args.ci_class, payload={
                "name": name, "short_description": f"{MARK} CI"
            })["sys_id"]
            print(f"created CI {name} ({args.ci_class})")
        if found is None and not args.dry_run:
            raise SystemExit(f"No configuration item is named {name}: create it, "
                             "or run again with --create-missing.")
        ids["ci"][name] = found or f"<{name}>"
    for key, table, name in (("service", "cmdb_ci_service", args.service),
                             ("group", "sys_user_group", args.group)):
        found = now.find(table, f"name={name}")
        if found is None and args.create_missing and not args.dry_run:
            found = now.call(table, payload={"name": name, "description": f"{MARK}"})["sys_id"]
            print(f"created {table} {name}")
        if found is None and not args.dry_run:
            raise SystemExit(f"No {table} record is named {name}: create it, "
                             "or run again with --create-missing.")
        ids[key] = found or f"<{name}>"
    return ids


def main():
    parser = argparse.ArgumentParser(
        description="Create the labelled CarePath incidents and changes in ServiceNow."
    )
    parser.add_argument("--instance", required=True, help="https://devNNNNNN.service-now.com")
    parser.add_argument("--user", required=True, help="an account that may create incidents")
    parser.add_argument("--password-file", help="file holding the password and nothing else")
    parser.add_argument("--service", default="CAREPATH_OPS",
                        help="business service on every record (the change connector's filter)")
    parser.add_argument("--group", default="CAREOPS",
                        help="assignment group on every incident (the incident connector's filter)")
    parser.add_argument("--api-ci", default="carepath-api-green",
                        help="the CI to write where the pack says carepath-api-blue")
    parser.add_argument("--close-code", default="Solution provided",
                        help="close code for the resolved incident, as the instance names it")
    parser.add_argument("--knowledge-base",
                        help="title of the knowledge base to publish the two articles in")
    parser.add_argument("--create-missing", "--create-cis", dest="create_missing",
                        action="store_true",
                        help="add the CIs, business service and assignment group the "
                             "instance does not have")
    parser.add_argument("--ci-class", default="cmdb_ci_appl",
                        help="the CI class a missing configuration item is created in")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every record and write nothing")
    args = parser.parse_args()
    if not args.password_file and not args.dry_run:
        raise SystemExit("--password-file is required unless --dry-run is given.")
    password = (
        # utf-8-sig: an editor that writes a byte-order mark first would otherwise
        # make it the password's first character, and every call a 401.
        Path(args.password_file).read_text(encoding="utf-8-sig").strip()
        if args.password_file else ""
    )
    if args.password_file and not password:
        raise SystemExit(f"{args.password_file} is empty: save the password in it first.")
    records = load(args.api_ci)
    now = ServiceNow(args.instance, args.user, password)
    offline = args.dry_run and not password
    if offline:
        ids = {"ci": {r["fields"]["CI"]: f"<{r['fields']['CI']}>" for r in records},
               "service": f"<{args.service}>", "group": f"<{args.group}>"}
    else:
        ids = references(now, records, args)
    created = skipped = 0
    for record in records:
        table, number = record["table"], record["fields"]["Number"]
        if not args.dry_run and now.find(table, f"correlation_id={record['sys_id']}"):
            print(f"skip    {number} ({table}): already in the instance")
            skipped += 1
            continue
        body = payload(record, ids, args.close_code)
        if args.dry_run:
            print(f"would create {table} {number}: {record['title']}")
            print("        " + json.dumps(body, sort_keys=True))
            continue
        made = now.call(table, payload=body)
        print(f"created {made.get('number', '?')} ({table}) for {number}: {record['title']}")
        created += 1
    if args.knowledge_base:
        made, kept = articles(now, ids, args, offline)
        created, skipped = created + made, skipped + kept
    if not args.dry_run:
        print(f"{created} created, {skipped} already there.")
        print("Sync the ServiceNow connectors in CarePathOps to import them.")
    return 0


def articles(now, ids, args, offline):
    """Publish `ARTICLES` in the named knowledge base: (created, already there).

    An article has no correlation id, so one with the same title in that knowledge
    base is the one already made. `offline` is a dry run with no password: nothing
    is looked up, and names stand in for sys_ids."""
    if offline:
        base = f"<{args.knowledge_base}>"
    else:
        base = now.find("kb_knowledge_base", f"title={args.knowledge_base}")
    if base is None:
        raise SystemExit(f"No knowledge base is titled {args.knowledge_base}: create it first.")
    created = skipped = 0
    for article in ARTICLES:
        ci = article["ci"].replace("carepath-api-blue", args.api_ci)
        body = {
            "short_description": article["title"],
            "text": article["text"],
            "kb_knowledge_base": base,
            "cmdb_ci": ids["ci"].get(ci, f"<{ci}>"),
            "workflow_state": "published",
            "valid_to": "2100-01-01",
            "meta": f"{article['keywords']} {MARK.lower()}",
        }
        if args.dry_run:
            print(f"would publish kb_knowledge: {article['title']}")
            print("        " + json.dumps(body, sort_keys=True))
            continue
        query = f"kb_knowledge_base={base}^short_description={article['title']}"
        if now.find("kb_knowledge", query):
            print(f"skip    article: {article['title']} (already in the knowledge base)")
            skipped += 1
            continue
        made = now.call("kb_knowledge", payload=body)
        print(f"created {made.get('number', '?')} (kb_knowledge): {article['title']}")
        created += 1
    return created, skipped


def window_check(records):
    """Each labelled change starts 0 to 48 hours before its incident on the same CI,
    as the operations graph requires to link them - checked before anything is sent."""
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    by_number = {record["fields"]["Number"]: record for record in records}
    for incident, label in pack["cases"].items():
        cause = by_number[label["cause"]]
        if cause["table"] != "change_request":
            continue
        opened = datetime.fromisoformat(by_number[incident]["fields"]["Opened"].replace("Z", ""))
        started = datetime.fromisoformat(cause["fields"]["Start"].replace("Z", ""))
        same_ci = cause["fields"]["CI"] == by_number[incident]["fields"]["CI"]
        if not (same_ci and timedelta(0) <= opened - started <= timedelta(days=2)):
            raise SystemExit(f"{label['cause']} would not link to {incident}.")


if __name__ == "__main__":
    window_check(load("carepath-api-green"))
    sys.exit(main())
