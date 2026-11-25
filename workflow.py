#
# Copyright IBM Corp. 2026
# SPDX-License-Identifier: Apache-2.0
#

"""Hire-to-onboard across an HR system, an identity server, and a task tracker.

Shared by ``run.py`` and the live test that keeps it honest. Three real systems:

    hris       Odoo (Recruitment + Employees)  job positions, applicants, employees
    identity   Keycloak                        the accounts people sign in with
    tracker    Directus                        the onboarding checklist

The HR coordinator's process, per hire (one case per *person*):

    0. look up the recruitment stages               call_operation:hr.recruitment.stage.search_read
    1. move the applicant through the stages        send_operation:hr.applicant.write
    2. hire them: Odoo creates the employee          send_operation:hr.applicant.create_employee_from_applicant
    3. open their onboarding checklist              send_operation:createItemsOnboardingTasks
    4. create their sign-in account                 send_operation:post_users
    5. close the account task                       send_operation:updateSingleItemsOnboardingTasks

The person has a different id in every system (an applicant id, an employee id,
a Keycloak user id, task ids), so the case is the one value all of them carry:
the work email. The correlator relates every call to it.

The process is mined two ways:

- **Observed**: the coordinator's calls through AgenticHarvest's tools, tapped live.
- **Imported**: each system's own history. Odoo's chatter records every stage
  change, Keycloak's admin events record account changes, and Directus's
  activity feed records task changes. These are read from the live systems
  after the fact and imported into the same log shape, so a process can be
  mined without anyone working through AgenticHarvest at all.

The policy the business states — no account before the hire is signed, and an
account within a day of the hire — is checked on every person.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from agenticharvest.core.events import (
    CompositeMapper,
    Correlator,
    KeyRule,
    ObjectRef,
    OcelLog,
    OcelTap,
    RecordMapping,
    import_records,
)
from agenticharvest.core.tools import InMemoryTap, ToolRegistry
from agenticharvest.domains.web import WebOCEL
from agenticharvest.domains.web.tools import client_for, web_tools
from agenticharvest.gateway.dispatch import ToolRequest, dispatch
from agenticharvest.mining import (
    BupaRMiner,
    Miner,
    ProcessModel,
    VariantMiner,
    collapse_repeats,
    traces,
)
from agenticharvest.mining.analysis import Rule

#: The configured web apps (see examples/systems).
HRIS, IDENTITY, TRACKER = "odoo", "keycloak", "directus"

#: The Keycloak realm accounts are created in (examples/systems/setup.py).
REALM = "agenticharvest"

#: The case: one per person, keyed by work email.
CASE_TYPE = "person"

#: The object types that are systems.
SYSTEMS = ("webapp",)

#: The onboarding checklist each hire gets.
CHECKLIST = ("laptop", "account", "payroll")

#: Each cohort is one week's hires: (name, email handle, role, stages before the
#: hire). Alan's account is created before his contract is signed, against policy.
COHORTS: dict[str, list[tuple[str, str, str, list[str]]]] = {
    "week-1": [
        ("Ada Byron", "ada.byron", "engineering", ["First Interview", "Contract Proposal"]),
        ("Grace Hopper", "grace.hopper", "engineering", ["First Interview", "Contract Proposal"]),
    ],
    "week-2": [
        ("Mary Jackson", "mary.jackson", "finance", ["First Interview", "Contract Proposal"]),
        ("Alan Turing", "alan.turing", "engineering",
         ["First Interview", "Second Interview", "Contract Proposal"]),
    ],
}

#: The hire whose account is created early.
EARLY_ACCOUNT = "alan.turing"

#: The coarsest timestamp any imported history keeps (Odoo's chatter: seconds).
HISTORY_RESOLUTION_SECONDS = 1.1

#: A hire the agent onboards, never seen while observing.
NEW_HIRE = ("Katherine Johnson", "katherine.johnson", "finance")

#: The business's onboarding policy, checked on every person. The first rule
#: catches an account created before the hire; the other two only judge
#: accounts created once the hire was made.
POLICY = [
    Rule(name="account only after the hire", kind="precedes",
         first="send_operation:hr.applicant.create_employee_from_applicant",
         then="send_operation:post_users"),
    Rule(name="every hire gets an account", kind="exists",
         first="send_operation:post_users"),
    Rule(name="checklist opened within a day of the hire", kind="within",
         first="send_operation:hr.applicant.create_employee_from_applicant",
         then="send_operation:createItemsOnboardingTasks", max_hours=24),
]

GOAL = (
    "You onboard a newly hired person. Their applicant record is in the HR system "
    "(web app 'odoo'). Move the applicant to the 'Contract Signed' stage, then hire "
    "them with hr.applicant.create_employee_from_applicant. Open their onboarding "
    "checklist in the tracker (web app 'directus'): one row each with task "
    "'laptop', 'account', and 'payroll', employee_ref set to their work email, "
    "owner 'it' for laptop and account and 'finance' for payroll, and status "
    "'open {tag}'. Create "
    "their sign-in account in the identity server (web app 'keycloak', realm "
    "'agenticharvest'): username = the part of their email before '@', email = their "
    "work email, firstName and lastName from their name, enabled true. Finally set "
    "the account task's status to 'done {tag}'."
)


def email_of(handle: str, tag: str) -> str:
    """A person's work email. ``tag`` keeps one run's people apart from another's."""
    return f"{handle}+{tag}@example.com"


# ----------------------------------------------------------------------
# Correlation: every call -> the person it is about
# ----------------------------------------------------------------------

_EMAIL = r"[A-Za-z0-9._%+-]+@example\.com"

KEY_RULES = [
    # The HR system: the email in the call (its vals or its context).
    KeyRule(object_type=CASE_TYPE, tools=["send_operation", "call_operation"],
            when={"app": HRIS}, param="body", pattern=_EMAIL, transform="lower"),
    # The identity server: the account's email.
    KeyRule(object_type=CASE_TYPE, tools=["send_operation", "call_operation"],
            when={"app": IDENTITY}, param="body.email", transform="lower"),
    KeyRule(object_type=CASE_TYPE, tools=["call_operation"], when={"app": IDENTITY},
            result=["body.*.email"], transform="lower"),
    # The tracker: the task's employee_ref.
    KeyRule(object_type=CASE_TYPE, tools=["send_operation", "call_operation"],
            when={"app": TRACKER}, param="body", pattern=_EMAIL, transform="lower"),
    KeyRule(object_type=CASE_TYPE, tools=["send_operation", "call_operation"],
            when={"app": TRACKER}, result=["body.data.employee_ref",
                                           "body.data.*.employee_ref"], transform="lower"),
]


def correlator() -> Correlator:
    """Relate every web call to its app and to the person it is about."""
    domains = {spec.name: "web" for spec, _ in web_tools()}
    inner = CompositeMapper({"web": WebOCEL(qualify_operations=True)}, domains)
    return Correlator(inner, KEY_RULES)


def observed_registry(
    log: OcelLog,
    clock: Callable[[], datetime] | None = None,
    records: InMemoryTap | None = None,
) -> ToolRegistry:
    """The web tools, every call recorded into ``log`` against its person.

    Args:
        records: Also keep each raw call here, e.g. to review an agent's work.
    """
    taps: list[Any] = [OcelTap(mapper=correlator(), log=log, clock=clock)]
    if records is not None:
        taps.append(records)
    registry = ToolRegistry(taps=taps)
    for spec, handler in web_tools():
        registry.register(spec, handler)
    return registry


def steady_clock(start: datetime | None = None, minutes: int = 20) -> Callable[[], datetime]:
    """A clock that ticks ``minutes`` per call, so policy windows are measurable."""
    ticks: Iterator[int] = iter(range(10**9))
    origin = start or datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    return lambda: origin + timedelta(minutes=minutes * next(ticks))


def _call(registry: ToolRegistry, tool: str, **params: Any) -> Any:
    result = dispatch(registry, ToolRequest(tool=tool, params=params))
    if not result.ok:
        raise RuntimeError(f"{tool} {params.get('name', '')} failed: {result.error}")
    return result.output["body"]


# ----------------------------------------------------------------------
# The live systems: direct, untapped setup done by other people
# ----------------------------------------------------------------------


def _odoo(model: str, method: str, **body: Any) -> Any:
    reply = client_for(HRIS).request("post", f"/json/2/{model}/{method}", body=body,
                                     max_chars=None)
    if not reply.ok:
        raise RuntimeError(f"{model}.{method}: HTTP {reply.status} {reply.body}")
    return reply.body


def open_requisition(role: str, tag: str) -> int:
    """The recruiter opens a job position (outside the observed process)."""
    return int(_odoo("hr.job", "create", vals_list=[{"name": f"{role.title()} ({tag})"}])[0])


def apply(name: str, email: str, job_id: int) -> int:
    """A candidate applies (outside the observed process); returns the applicant id."""
    return int(_odoo("hr.applicant", "create", vals_list=[
        {"partner_name": name, "email_from": email, "job_id": job_id}])[0])


# ----------------------------------------------------------------------
# The coordinator's process
# ----------------------------------------------------------------------


def onboard_by_hand(
    registry: ToolRegistry,
    applicant_id: int,
    name: str,
    email: str,
    stages: list[str],
    *,
    tag: str,
    account_early: bool = False,
) -> dict[str, Any]:
    """Play the HR coordinator for one hire, through the real tools.

    Args:
        account_early: Create the sign-in account before the hire is signed, as
            a rushed coordinator sometimes does. The policy check catches it.

    Returns:
        The ids each system gave this person.
    """
    def create_account() -> None:
        first, _, last = name.partition(" ")
        _call(registry, "send_operation", app=IDENTITY, name="post_users",
              path_params={"realm": REALM},
              body={"username": email.split("@")[0], "email": email,
                    "firstName": first, "lastName": last, "enabled": True},
              confirm=True)

    if account_early:
        create_account()
        # Odoo's chatter stamps changes to the whole second. A person does this
        # minutes apart; a script does it within one second, which would leave
        # the imported history unable to order the account against the hire.
        time.sleep(HISTORY_RESOLUTION_SECONDS)
    # The email rides in each call's context so the correlator can place the
    # call on the person; Odoo ignores context keys it does not use.
    stages_found = _call(registry, "call_operation", app=HRIS,
                         name="hr.recruitment.stage.search_read",
                         body={"domain": [], "fields": ["name"], "context": {"email": email}})
    stage_id = {s["name"]: s["id"] for s in stages_found}
    for stage in [*stages, "Contract Signed"]:
        _call(registry, "send_operation", app=HRIS, name="hr.applicant.write",
              body={"ids": [applicant_id], "vals": {"stage_id": stage_id[stage]},
                    "context": {"email": email}},
              confirm=True)
    _call(registry, "send_operation", app=HRIS,
          name="hr.applicant.create_employee_from_applicant",
          body={"ids": [applicant_id], "context": {"email": email}}, confirm=True)
    employee_id = _odoo("hr.applicant", "read", ids=[applicant_id],
                        fields=["employee_id"])[0]["employee_id"][0]

    created = _call(registry, "send_operation", app=TRACKER, name="createItemsOnboardingTasks",
                    body=[{"employee_ref": email, "task": item,
                           "owner": "finance" if item == "payroll" else "it",
                           "status": f"open {tag}"} for item in CHECKLIST],
                    confirm=True)
    tasks = {row["task"]: row["id"] for row in created["data"]}

    if not account_early:
        create_account()
    _call(registry, "send_operation", app=TRACKER, name="updateSingleItemsOnboardingTasks",
          path_params={"id": tasks["account"]},
          body={"status": f"done {tag}", "employee_ref": email}, confirm=True)
    return {"applicant": applicant_id, "employee": employee_id, "tasks": tasks}


def observe(log: OcelLog, *, tag: str) -> dict[str, dict[str, Any]]:
    """Stage 1: observe the coordinator onboard every cohort's hires."""
    registry = observed_registry(log, clock=steady_clock())
    hires: dict[str, dict[str, Any]] = {}
    jobs: dict[str, int] = {}
    for cohort in COHORTS.values():
        for name, handle, role, stages in cohort:
            if role not in jobs:
                jobs[role] = open_requisition(role, tag)
            email = email_of(handle, tag)
            applicant = apply(name, email, jobs[role])
            hires[email] = onboard_by_hand(
                registry, applicant, name, email, stages, tag=tag,
                account_early=handle == EARLY_ACCOUNT,
            )
    hires["_jobs"] = jobs
    return hires


def default_miner() -> Miner:
    """bupaR (which adds a BPMN) when R is installed, else the variant miner."""
    bupar = BupaRMiner(case_type=CASE_TYPE)
    return bupar if bupar.available() else VariantMiner(case_type=CASE_TYPE)


def mine(log: OcelLog, miner: Miner | None = None) -> ProcessModel:
    """Stage 2: mine one case per person, folding repeated stage moves."""
    return (miner or default_miner()).mine(collapse_repeats(log, case_type=CASE_TYPE))


def case_traces(log: OcelLog) -> dict[str, list[str]]:
    """Each person's trace, with repeated steps collapsed."""
    return traces(collapse_repeats(log, case_type=CASE_TYPE), case_type=CASE_TYPE)


# ----------------------------------------------------------------------
# Passive mining: each system's own history
# ----------------------------------------------------------------------

#: Odoo's chatter: one record per tracked change, with the person's email
#: joined in by :func:`odoo_history`.
ODOO_HISTORY = RecordMapping(
    time="date",
    activity="hris:{change}",
    objects=[ObjectRef(type="webapp", value=HRIS, constant=True),
             ObjectRef(type=CASE_TYPE, path="email", qualifier="about")],
    attributes={"record": "res_id"},
    source="odoo chatter",
)

#: Keycloak's admin events: one per account change.
KEYCLOAK_HISTORY = RecordMapping(
    time="time",
    time_format="epoch-ms",
    activity="identity:{operationType}:{resourceType}",
    lowercase_activity=True,
    objects=[ObjectRef(type="webapp", value=IDENTITY, constant=True),
             ObjectRef(type=CASE_TYPE, path="representation.email", qualifier="about")],
    source="keycloak admin events",
)

#: Directus's activity feed: one per item change, revisions carrying the data.
DIRECTUS_HISTORY = RecordMapping(
    time="timestamp",
    activity="tracker:{action}:{collection}",
    objects=[ObjectRef(type="webapp", value=TRACKER, constant=True),
             ObjectRef(type=CASE_TYPE, path="revisions.*.data.employee_ref", qualifier="about")],
    include={"collection": "onboarding_tasks"},
    source="directus activity",
)


def odoo_history(emails: dict[int, str], since: str) -> list[dict[str, Any]]:
    """Odoo's chatter for the given applicants, one record per change.

    Args:
        emails: Applicant id -> work email (the chatter names the applicant
            record, not the person).
        since: Only changes at or after this time (``YYYY-MM-DD HH:MM:SS``).
    """
    messages = _odoo("mail.message", "search_read",
                     domain=[["model", "=", "hr.applicant"], ["res_id", "in", list(emails)],
                             ["date", ">=", since]],
                     fields=["date", "res_id", "subtype_id", "tracking_value_ids"],
                     order="date asc, id asc")
    values = {}
    tracking = [t for m in messages for t in m["tracking_value_ids"]]
    if tracking:
        values = {t["id"]: t for t in _odoo(
            "mail.tracking.value", "read", ids=tracking,
            fields=["field_id", "new_value_char"])}
    records = []
    for message in messages:
        email = emails[message["res_id"]]
        stage = next((values[t]["new_value_char"] for t in message["tracking_value_ids"]
                      if t in values and "Stage" in values[t]["field_id"][1]), None)
        change = f"stage {stage}" if stage else (message["subtype_id"] or [0, "note"])[1].lower()
        records.append({**message, "email": email, "change": change})
    return records


def keycloak_history(since_ms: int, page: int = 200) -> list[dict[str, Any]]:
    """Keycloak's admin events in the realm since ``since_ms``, with representations parsed."""
    import json

    day = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    events: list[dict[str, Any]] = []
    first = 0
    while True:
        reply = client_for(IDENTITY).request(
            "get", f"/admin/realms/{REALM}/admin-events",
            query={"dateFrom": day, "first": first, "max": page}, max_chars=None)
        batch = reply.body if isinstance(reply.body, list) else []
        for event in batch:
            if int(event["time"]) < since_ms:
                continue
            rep = event.get("representation")
            event["representation"] = json.loads(rep) if isinstance(rep, str) and rep else {}
            events.append(event)
        if len(batch) < page:
            break
        first += page
    return sorted(events, key=lambda e: e["time"])


def directus_history(since_iso: str) -> list[dict[str, Any]]:
    """Directus's activity on onboarding tasks since ``since_iso``, with revision data."""
    reply = client_for(TRACKER).request("get", "/activity", query={
        "filter": {"collection": {"_eq": "onboarding_tasks"},
                   "timestamp": {"_gte": since_iso}},
        "fields": "action,collection,item,timestamp,revisions.data",
        "sort": "timestamp", "limit": -1,
    }, max_chars=None)
    return list((reply.body or {}).get("data", []))


def import_history(
    hires: dict[str, dict[str, Any]], since: datetime
) -> OcelLog:
    """Stage 3: rebuild the process from the three systems' own histories."""
    log = OcelLog()
    applicants = {h["applicant"]: email for email, h in hires.items() if not email.startswith("_")}
    import_records(log, odoo_history(applicants, since.strftime("%Y-%m-%d %H:%M:%S")),
                   ODOO_HISTORY, id_prefix="odoo-")
    import_records(log, keycloak_history(int(since.timestamp() * 1000)),
                   KEYCLOAK_HISTORY, id_prefix="kc-")
    import_records(log, directus_history(since.strftime("%Y-%m-%dT%H:%M:%S")),
                   DIRECTUS_HISTORY, id_prefix="dx-")
    return log


# ----------------------------------------------------------------------
# Cleanup: everything one run created, on every system
# ----------------------------------------------------------------------


def cleanup(tag: str) -> dict[str, int]:
    """Remove the people, accounts, and tasks one run created; return counts."""
    removed = {"accounts": 0, "tasks": 0, "employees": 0, "applicants": 0, "jobs": 0}
    # Keycloak's free-text search drops the '+'; its email filter matches a substring.
    users = client_for(IDENTITY).request(
        "get", f"/admin/realms/{REALM}/users", query={"email": f"+{tag}@", "max": 1000})
    for user in users.body or []:
        if f"+{tag}@" in str(user.get("email", "")):
            client_for(IDENTITY).request("delete", f"/admin/realms/{REALM}/users/{user['id']}")
            removed["accounts"] += 1
    tasks = client_for(TRACKER).request("get", "/items/onboarding_tasks", query={
        "filter": {"employee_ref": {"_contains": f"+{tag}@"}}, "limit": -1, "fields": "id"})
    ids = [row["id"] for row in (tasks.body or {}).get("data", [])]
    if ids:
        client_for(TRACKER).request("delete", "/items/onboarding_tasks", body=ids)
        removed["tasks"] = len(ids)
    pattern = f"+{tag}@"
    for model, field, key in (("hr.employee", "work_email", "employees"),
                              ("hr.applicant", "email_from", "applicants")):
        found = _odoo(model, "search_read", domain=[[field, "ilike", pattern]], fields=["id"],
                      context={"active_test": False})
        if found:
            _odoo(model, "unlink", ids=[r["id"] for r in found])
            removed[key] = len(found)
    jobs = _odoo("hr.job", "search_read", domain=[["name", "ilike", f"({tag})"]], fields=["id"])
    if jobs:
        _odoo("hr.job", "unlink", ids=[r["id"] for r in jobs])
        removed["jobs"] = len(jobs)
    return removed
