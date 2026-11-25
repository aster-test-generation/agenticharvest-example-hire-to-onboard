#!/usr/bin/env python
#
# Copyright IBM Corp. 2026
# SPDX-License-Identifier: Apache-2.0
#
"""Observe -> import -> mine -> check policy -> agentify, across three live HR systems.

Run it from the repo root, with the live systems up (see examples/systems)::

    uv run python examples/hire_to_onboard/run.py            # stages 1-4, no LLM
    uv run python examples/hire_to_onboard/run.py --agent    # + stage 5, live LLM

    1. OBSERVE    an HR coordinator onboards four hires across Odoo (HR),
                  Keycloak (sign-in accounts), and Directus (the onboarding
                  checklist). One log, one case per person, keyed by email.
    2. IMPORT     the same process rebuilt from the three systems' own
                  histories (Odoo chatter, Keycloak admin events, Directus
                  activity), with no tapping at all.
    3. MINE       the dominant process, the deviating case, and the
                  object-centric view: people, systems, and how they meet.
    4. POLICY     "no account before the hire is signed" and "an account
                  within a day of the hire", checked on every person.
    5. AGENTIFY   (--agent) a live LLM onboards a new hire by the mined plan;
                  its work is checked in all three systems and against policy.

Everything a run creates (applicants, employees, jobs, accounts, tasks) is
removed from the live systems at the end, unless --keep is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The sibling module, made importable by the sys.path line above.
from workflow import (
    CASE_TYPE,
    GOAL,
    NEW_HIRE,
    POLICY,
    SYSTEMS,
    apply,
    case_traces,
    cleanup,
    email_of,
    import_history,
    mine,
    observe,
    observed_registry,
    open_requisition,
    steady_clock,
)

from agenticharvest.core.events import OcelLog
from agenticharvest.core.tools import InMemoryTap
from agenticharvest.domains.web.config import WebConfigError
from agenticharvest.domains.web.tools import client_for, web_tools
from agenticharvest.mining import ProcessModel, check_trace
from agenticharvest.mining.analysis import check_rules, handoffs, object_centric_dfg
from agenticharvest.orchestration.compile import compile_plan, restrict_tools


def _banner(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def print_model(model: ProcessModel) -> None:
    """Print what the miner found beyond the variants: frequencies, edges, BPMN.

    Steps are listed in the order the dominant variant takes them (miners
    differ: bupaR sorts alphabetically), so the printout reads as the process.
    """
    dominant = max(model.variants, key=lambda v: v.frequency).sequence if model.variants else []
    rank = {name: i for i, name in enumerate(dominant)}
    print("\n  activities (how often each step happened):")
    for activity in sorted(model.activities, key=lambda a: rank.get(a.name, 99)):
        print(f"    {activity.frequency:3}  {activity.name}")
    print("  directly-follows (which step came straight after which):")
    for edge in sorted(model.directly_follows,
                       key=lambda e: (rank.get(e.source, 99), rank.get(e.target, 99))):
        print(f"    {edge.frequency:3}  {edge.source} -> {edge.target}")
    if model.bpmn is not None:
        tasks = sum(n.type == "task" for n in model.bpmn.nodes)
        print(f"  BPMN: {len(model.bpmn.nodes)} nodes ({tasks} tasks), "
              f"{len(model.bpmn.flows)} sequence flows")
    else:
        print("  BPMN: none (install R and bupaR for BPMN discovery)")


def _person(case: str) -> str:
    return case.split(":", 1)[1].split("+", 1)[0]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("hire_to_onboard_out"))
    parser.add_argument("--agent", action="store_true", help="run stage 5 with a live LLM")
    parser.add_argument("--keep", action="store_true",
                        help="leave this run's records in the live systems")
    args = parser.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:6]

    try:
        return _run(args, out, tag)
    except WebConfigError as exc:
        print(f"\nA live system is not configured: {exc}")
        print("Start the live systems and run examples/systems/setup.py first.")
        return 1
    finally:
        if not args.keep:
            removed = cleanup(tag)
            print("\n  cleaned up: " + ", ".join(f"{n} {k}" for k, n in removed.items() if n))


def _run(args: argparse.Namespace, out: Path, tag: str) -> int:
    since = datetime.now(timezone.utc) - timedelta(seconds=5)

    _banner("1. OBSERVE — a coordinator onboards four hires across three systems")
    log = OcelLog()
    hires = observe(log, tag=tag)
    for email, ids in hires.items():
        if not email.startswith("_"):
            print(f"  {_person('p:' + email):13} applicant {ids['applicant']}, employee "
                  f"{ids['employee']}, tasks {sorted(ids['tasks'].values())}")
    (out / "observed.ocel.json").write_text(json.dumps(log.to_ocel2(), indent=2, default=str))
    print(f"\n  {len(log.events)} tool calls -> {out / 'observed.ocel.json'}")

    _banner("2. IMPORT — the same process from the systems' own histories")
    imported = import_history(hires, since)
    (out / "imported.ocel.json").write_text(
        json.dumps(imported.to_ocel2(), indent=2, default=str))
    sources: dict[str, int] = {}
    for event in imported.events:
        sources[event.attributes["source"]] = sources.get(event.attributes["source"], 0) + 1
    for source, count in sources.items():
        print(f"  {count:3} events from {source}")
    for case, trace in list(case_traces(imported).items())[:1]:
        print(f"\n  {_person(case)}, as the systems recorded it:\n    " + "\n    ".join(trace))

    _banner("3. MINE — the process, its deviations, and the object-centric view")
    model = mine(log)
    (out / "process_model.json").write_text(model.model_dump_json(indent=2))
    print(f"  from the observed calls — miner: {model.miner} · people: {model.n_cases} · "
          f"events: {model.n_events}")
    for variant in model.variants:
        print(f"  variant x{variant.frequency}: {' -> '.join(variant.sequence)}")
    print_model(model)
    print(f"  -> {out / 'process_model.json'}")

    history = mine(imported)
    (out / "imported_model.json").write_text(history.model_dump_json(indent=2))
    print(f"\n  from the systems' own histories — miner: {history.miner} · people: "
          f"{history.n_cases} · events: {history.n_events}")
    for variant in history.variants:
        print(f"  variant x{variant.frequency}: {' -> '.join(variant.sequence)}")
    print(f"  -> {out / 'imported_model.json'}")

    print()
    for handoff in handoffs(log, CASE_TYPE, SYSTEMS):
        print(f"  handoff  {handoff.source} -> {handoff.target}: {handoff.cases} people")
    dfg = object_centric_dfg(imported, [CASE_TYPE, "webapp"])
    print(f"  object-centric view: {dfg.object_types}; activities shared by people and "
          f"systems: {sum(len(t) > 1 for t in dfg.activities.values())}")

    _banner("4. POLICY — onboarding rules, checked on every person")
    report = check_rules(log, CASE_TYPE, POLICY)
    for rule, passed in report.passed.items():
        print(f"  {passed}/{report.cases} pass  {rule}")
    for violation in report.violations:
        print(f"  VIOLATION  {_person(violation.case)}: {violation.rule} — {violation.detail}")

    specs = [spec for spec, _ in web_tools()]
    plan = compile_plan(model, specs, name="hire-to-onboard")
    prompt = plan.system_prompt(GOAL.format(tag=tag))
    (out / "agent_prompt.txt").write_text(prompt)
    print(f"\n  compiled plan: {len(plan.steps)} steps, {plan.coverage:.0%} of observed hires, "
          f"tools {plan.tools}")

    if not args.agent:
        print("\nRe-run with --agent to hand the plan to a live LLM (stage 5).")
        return 0

    _banner("5. AGENTIFY — a live agent onboards a new hire")
    from agenticharvest.orchestration.agents.adapters import registry_tools
    from agenticharvest.orchestration.agents.base import AgentConfig
    from agenticharvest.orchestration.agents.factory import AgentFactory, Backend
    from agenticharvest.utils.llm import LLMConfig, LLMConfigError

    try:
        llm = LLMConfig.from_env()
    except LLMConfigError as exc:
        print(f"  no LLM endpoint configured ({exc}); skipping stage 5.")
        return 1

    name, handle, role = NEW_HIRE
    email = email_of(handle, tag)
    applicant = apply(name, email, open_requisition(role, tag))
    agent_log = OcelLog()
    calls = InMemoryTap()
    registry = observed_registry(agent_log, clock=steady_clock(), records=calls)
    agent = AgentFactory.create(
        Backend.OPENAI_COMPATIBLE,
        AgentConfig(model=llm.model, system_prompt=prompt, max_tokens=2048, max_retries=30),
        tools=restrict_tools(registry_tools(registry), plan),
        llm_config=llm,
    )

    async def go() -> str:
        try:
            result = await agent.run(
                f"Onboard {name} (work email {email}). Their applicant id in the HR "
                f"system is {applicant}. In every HR system call, include "
                f"\"context\": {{\"email\": \"{email}\"}} in the body."
            )
        finally:
            await agent.close()
        return result.content

    print(asyncio.run(go()))
    (out / "agent.ocel.json").write_text(json.dumps(agent_log.to_ocel2(), indent=2, default=str))
    (out / "agent_calls.json").write_text(json.dumps(
        [{"tool": r.tool, "params": r.params, "ok": r.result.ok, "error": r.result.error}
         for r in calls.records], indent=2, default=str))
    return _check_agent(agent_log, model, applicant, email, tag)


def _check_agent(
    agent_log: OcelLog, model: ProcessModel, applicant: int, email: str, tag: str
) -> int:
    """Check the agent's work in each live system, its trace, and the policy."""
    odoo = client_for("odoo")
    record = odoo.request("post", "/json/2/hr.applicant/read",
                          body={"ids": [applicant], "fields": ["stage_id", "employee_id"]}).body[0]
    users = client_for("keycloak").request(
        "get", "/admin/realms/agenticharvest/users", query={"email": email, "exact": True}).body
    tasks = client_for("directus").request("get", "/items/onboarding_tasks", query={
        "filter": {"employee_ref": {"_eq": email}}, "limit": -1}).body["data"]
    status = {t["task"]: t["status"] for t in tasks}
    checks = {
        "hired in the HR system": bool(record["employee_id"])
        and record["stage_id"][1] == "Contract Signed",
        "one sign-in account": len(users) == 1,
        "laptop, account, payroll tasks": sorted(status) == ["account", "laptop", "payroll"],
        "account task done": status.get("account") == f"done {tag}",
    }
    print("\n  the agent's work, read back from the live systems:")
    for check, ok in checks.items():
        print(f"    {'ok ' if ok else 'MISSING'} {check}")

    traces = case_traces(agent_log)
    for case, trace in traces.items():
        report = check_trace(model, trace)
        print(f"\n  {_person(case)}: {' -> '.join(trace)}")
        print(f"  conforms: {report.conforms} · fitness: {report.fitness:.2f}")
    policy = check_rules(agent_log, CASE_TYPE, POLICY)
    print(f"  policy: {'all rules pass' if policy.ok else policy.violations}")
    # Success is the outcome: right records in every system, within policy. A
    # trace that retried a call is reported above but does not fail the run.
    return 0 if all(checks.values()) and policy.ok and traces else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
