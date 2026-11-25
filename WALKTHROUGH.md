# Hire to onboard: the full walkthrough

This page walks through the whole example, stage by stage: what it sets up,
what each step does, the output you will see, the files it writes, and why each
stage matters. Every output shown here was copied from a real run against the
live systems. Ids, tags, and timestamps will differ in your run; the shape and
the results will not.

For a short overview, see [README.md](README.md).

**Contents**

1. [The problem](#1-the-problem)
2. [What runs where](#2-what-runs-where)
3. [Set up and run](#3-set-up-and-run)
4. [Stage 1: Observe](#4-stage-1-observe)
5. [Stage 2: Import the systems' own history](#5-stage-2-import-the-systems-own-history)
6. [Stage 3: Mine](#6-stage-3-mine)
7. [Stage 4: Check the policy](#7-stage-4-check-the-policy)
8. [Stage 5: Agentify](#8-stage-5-agentify)
9. [Cleanup](#9-cleanup)
10. [The output files](#10-the-output-files)
11. [Look at it yourself](#11-look-at-it-yourself)
12. [What the safety checks do](#12-what-the-safety-checks-do)
13. [Change it](#13-change-it)
14. [Troubleshooting](#14-troubleshooting)
15. [How the code is organized](#15-how-the-code-is-organized)

---

## 1. The problem

When a company hires someone, an HR coordinator does the same steps each
time, across three systems:

| System | Product | What the coordinator does there |
|---|---|---|
| **HR system** | Odoo 19 (Recruitment + Employees) | Moves the applicant through the interview stages, then hires them. Odoo creates the employee record. |
| **Task tracker** | Directus 11 | Opens the new hire's onboarding checklist: three tasks (laptop, account, payroll). |
| **Identity server** | Keycloak 26 | Creates the account the new hire signs in with, then ticks off the "account" task. |

The company has an onboarding **policy**:

- No sign-in account before the hire is made (the contract is signed).
- Every hire gets an account.
- The checklist is opened within a day of the hire.

**Why it is hard to see this process.** The new hire has a **different id in
every system**:

| System | The person's id there |
|---|---|
| Odoo | an applicant id (e.g. `120`), then an employee id (`122`) |
| Keycloak | a user id (a UUID, e.g. `7a50cbb6-…`) |
| Directus | three task ids (`352`, `353`, `354`) |

No id appears in more than one system. Each system's log shows its own
fragment, and checking the policy needs all three together. "Was the account
created after the hire?" compares a Keycloak event with an Odoo event.

This example joins the three systems on the one value all of them carry, the
**work email**. It mines the process two ways: from the coordinator's calls,
and from the three systems' own audit trails. It checks the policy on every
person and catches a real violation. Then it has an AI agent onboard a new
hire, and checks the agent's work in all three systems.

## 2. What runs where

```
  your machine
  ┌───────────────────────────────────────────────────────────────────────────┐
  │                                                                           │
  │  run.py  (stages 1-5)                                                     │
  │    │                                                                      │
  │    │  every call: the web tools, through one tapped AgenticHarvest registry     │
  │    ▼                                                                      │
  │  ┌───────────────┐       ┌───────────────┐       ┌───────────────┐        │
  │  │ Odoo   :8069  │       │ Keycloak :8080│       │ Directus :8055│        │
  │  │ HR system     │       │ identity      │       │ task tracker  │        │
  │  │ JSON-2 API    │       │ Admin REST API│       │ REST API      │        │
  │  │ chatter log   │       │ admin events  │       │ activity feed │        │
  │  └───────┬───────┘       └───────┬───────┘       └───────┬───────┘        │
  │          └─── stage 2 reads each system's own history ───┘                │
  │                                                                           │
  │  observed log (tapped calls)   +   imported log (systems' history)        │
  │  both keyed by person = work email                                        │
  └───────────────────────────────────────────────────────────────────────────┘
```

All three apps are real, unmodified products running in containers. AgenticHarvest
built none of their tools by hand. It reads each app's own description of its
API:

| App | Description AgenticHarvest reads | Tools it builds (examples) |
|---|---|---|
| Odoo | Odoo's `/doc/<model>.json` self-description | `hr.applicant.write`, `hr.applicant.create_employee_from_applicant`, `hr.recruitment.stage.search_read` |
| Keycloak | The vendor's published Admin REST API (OpenAPI 3) | `post_users`, `get_users`, `delete_users_by_user_id` |
| Directus | Its live OpenAPI description (`/server/specs/oas`) | `createItemsOnboardingTasks`, `updateSingleItemsOnboardingTasks` |

## 3. Set up and run

There are two ways. Both give the same output.

### A. Containers only (no Python on your machine)

You need [Podman](https://podman.io/docs/installation) or Docker, git, and
about 4 GB of free memory.

```bash
git clone https://github.com/aster-test-generation/agenticharvest-example-hire-to-onboard.git
cd agenticharvest-example-hire-to-onboard
./run.sh                 # Windows: .\run.ps1
```

On the first run, `run.sh`:

1. Finds Podman (or Docker) and checks it is running.
2. Fetches AgenticHarvest into `./.agenticharvest`, with the live stack as a submodule.
3. Starts Odoo, Keycloak, and Directus in containers. The first start pulls
   about 2 GB of images and initializes Odoo's database, which takes a few
   minutes. Later runs take seconds.
4. Builds the **runner** image: Python with AgenticHarvest and this example installed.
5. Provisions the apps (`setup.py`): an Odoo API key, the Keycloak `agenticharvest`
   realm with admin-event auditing on, and the Directus `onboarding_tasks`
   collection and API token.
6. Runs `run.py` inside the runner.

You will see this before the example's own output:

```
Provisioning the live systems (examples/systems/setup.py)...
Checking each app through AgenticHarvest:
  directus   ok      Dynamic API Specification …: 2 resources, 14 operations (10 write)
  keycloak   ok      Keycloak Admin REST API 1.0: 50 resources, 369 operations (181 write)
  odoo       ok      Odoo 19.0-…: 5 resources, 51 operations (41 write)
```

Each line means AgenticHarvest reached the app and built its tools from the app's
own description. "(41 write)" is how many of its operations change data: every
one of them is gated.

### B. With Python and uv (for developers)

From the AgenticHarvest repo root, cloned with `--recurse-submodules`:

```bash
uv sync --extra web --extra openai_compatible --extra dev
podman compose -f examples/systems/compose.yaml up -d
uv run python examples/systems/setup.py
uv run python examples/hire_to_onboard/run.py
```

`setup.py` writes each app's address and credentials to `.env` in the repo
root. Run scripts from the repo root, so they find that file.

### Options

| Option | What it does |
|---|---|
| *(none)* | Stages 1 to 4. No LLM needed. Takes about 3 seconds once the stack is up. |
| `--agent` | Adds stage 5: a live LLM onboards a new hire. Takes 15 to 40 seconds. |
| `--keep` | Leaves this run's records in all three apps, so you can look at them. |
| `--out DIR` | Where the output files go (host runs). Default: `hire_to_onboard_out/`. With `run.sh`, files go to `out/hire_to_onboard/` next to it; set `AGENTICHARVEST_OUT` to change that. |

For `--agent`, set an OpenAI-compatible endpoint (OpenAI, a LiteLLM proxy,
vLLM, or a local server) in the environment or in `.env`:

```bash
API_BASE=https://your-endpoint/v1
API_KEY=your-key
MODEL=your-model
```

In the containers-only setup, put those three lines in a `.env` file next to
`run.sh`.

---

## 4. Stage 1: Observe

**What happens.** The script plays the HR coordinator for two weeks of hires,
four people in all. For each person, first the recruiter (outside the observed
process, directly in Odoo):

- opens a job position for the role, e.g. `Engineering (49c7b1)`;
- adds the candidate as an applicant, with their work email.

Then the coordinator, through AgenticHarvest's tools, every call tapped:

| # | Step | Tool call | System |
|---|---|---|---|
| 0 | Look up the recruitment stages | `call_operation` `hr.recruitment.stage.search_read` | Odoo |
| 1 | Move the applicant through the interview stages, to "Contract Signed" | `send_operation` `hr.applicant.write` (one per stage) | Odoo |
| 2 | Hire them: Odoo creates the employee | `send_operation` `hr.applicant.create_employee_from_applicant` | Odoo |
| 3 | Open the onboarding checklist: laptop, account, payroll | `send_operation` `createItemsOnboardingTasks` | Directus |
| 4 | Create their sign-in account | `send_operation` `post_users` | Keycloak |
| 5 | Mark the "account" task done | `send_operation` `updateSingleItemsOnboardingTasks` | Directus |

**The deviation.** One of the four, Alan Turing, gets his account created
**first**, before his interviews and before he is hired. A rushed coordinator
does this sometimes. It breaks the policy, and stages 3 to 5 catch it.

**Unique emails.** Each run tags every email, e.g.
`ada.byron+49c7b1@example.com`, so one run's people never collide with
another's, and cleanup can find them.

**Output:**

```
========================================================================
1. OBSERVE — a coordinator onboards four hires across three systems
========================================================================
  ada.byron     applicant 120, employee 122, tasks [352, 353, 354]
  grace.hopper  applicant 121, employee 123, tasks [355, 356, 357]
  mary.jackson  applicant 122, employee 124, tasks [358, 359, 360]
  alan.turing   applicant 123, employee 125, tasks [361, 362, 363]

  33 tool calls -> hire_to_onboard_out/observed.ocel.json
```

(Paths in the output are where *this* run wrote its files. See
[section 10](#10-the-output-files) for where yours go.)

**Reading it.** Each line is one person, with the ids each system gave them:
the Odoo applicant and employee ids, and the three Directus task ids. These
ids are all different, and none of them identifies the person anywhere else.

**33 tool calls.** Each person makes one stage look-up, three or four stage
moves (Alan has an extra "Second Interview"), the hire, the checklist, the
account, and the task update: 3 × 8 + 9 = 33.

### How one log covers three systems

**The case is the work email.** The correlator's rules say where the email
appears in each system's calls:

```python
KEY_RULES = [
    # Odoo: the email in the call body (its vals, or its context).
    KeyRule(object_type="person", tools=["send_operation", "call_operation"],
            when={"app": "odoo"}, param="body", pattern=EMAIL, transform="lower"),
    # Keycloak: the account's email.
    KeyRule(object_type="person", tools=["send_operation", "call_operation"],
            when={"app": "keycloak"}, param="body.email", transform="lower"),
    # Directus: the task's employee_ref.
    KeyRule(object_type="person", tools=["send_operation", "call_operation"],
            when={"app": "directus"}, param="body", pattern=EMAIL, transform="lower"),
    …
]
```

`when={"app": …}` makes each rule apply to one system only. `transform="lower"`
makes `Ada.Byron@…` and `ada.byron@…` the same person.

**Odoo's calls name records by id, not email.** A `write` to applicant `120`
doesn't mention the email. So the coordinator puts it in the call's
**context**, a dictionary every Odoo call accepts and ignores unknown keys of:

```json
{ "ids": [120], "vals": { "stage_id": 6 }, "context": { "email": "ada.byron+49c7b1@example.com" } }
```

The correlator's Odoo rule then finds it. The same trick works for any API that
accepts free-form metadata on a call.

Here is the hire event for Ada, from `observed.ocel.json`:

```json
{ "id": "e5", "type": "send_operation:hr.applicant.create_employee_from_applicant",
  "time": "2026-10-05T10:20:00Z",
  "attributes": [ { "name": "app", "value": "odoo" },
                  { "name": "resource", "value": "hr.applicant" },
                  { "name": "records", "value": ["…"] }, { "name": "status", "value": 200 } ],
  "relationships": [
    { "objectId": "webapp:odoo",                                              "qualifier": "target" },
    { "objectId": "operation:odoo:hr.applicant.create_employee_from_applicant", "qualifier": "invokes" },
    { "objectId": "resource:odoo:hr.applicant",                               "qualifier": "writes" },
    { "objectId": "person:ada.byron+49c7b1@example.com",                      "qualifier": "writes" } ] }
```

Every event, in any of the three systems, relates to the same
`person:ada.byron+…` object. That is what makes one process out of three
systems.

**Timestamps.** The script uses a steady clock that ticks 20 minutes per call,
so the policy's time window is measurable. In a real deployment, the tap uses
the real time.

---

## 5. Stage 2: Import the systems' own history

**What this stage shows.** Stage 1 only works if the work goes **through**
AgenticHarvest's tools. Most of the time, it doesn't: people use the apps directly.
But every one of these apps already keeps its own history. This stage rebuilds
the same process from those histories, with **no tapping at all**.

**What happens.** After stage 1, the script reads each system's audit trail
from its live API:

| System | Its history | Read with | One record per |
|---|---|---|---|
| Odoo | The chatter: messages and tracked field changes on each applicant | `mail.message` and `mail.tracking.value`, via JSON-2 | change: "new applicant", "Stage changed to X" |
| Keycloak | Admin events (setup.py turned them on for the realm) | `GET /admin/realms/agenticharvest/admin-events` | account change: create, update, delete |
| Directus | The activity feed, with each change's data | `GET /activity` | item change: create, update, delete |

Each source is turned into events by a declarative **`RecordMapping`**. It
says where the time is, how to name the activity, and which field names the
person. The Keycloak one:

```python
KEYCLOAK_HISTORY = RecordMapping(
    time="time", time_format="epoch-ms",                       # milliseconds since 1970
    activity="identity:{operationType}:{resourceType}",        # e.g. identity:create:user
    lowercase_activity=True,
    objects=[ObjectRef(type="webapp", value="keycloak", constant=True),
             ObjectRef(type="person", path="representation.email")],
)
```

The Directus one finds the person inside the change's saved data, with the path
`revisions.*.data.employee_ref`. The Odoo one names the person by joining each
message's applicant id back to its email.

**Output:**

```
========================================================================
2. IMPORT — the same process from the systems' own histories
========================================================================
   21 events from odoo chatter
    4 events from keycloak admin events
   16 events from directus activity

  ada.byron, as the systems recorded it:
    hris:new applicant
    hris:stage First Interview
    hris:stage Contract Proposal
    hris:stage Contract Signed
    hris:note
    tracker:create:onboarding_tasks
    identity:create:user
    tracker:update:onboarding_tasks
```

**Reading it.**

- **21 from Odoo.** One "new applicant" message, one per stage change, and one
  note (Odoo's "employee created" message when the hire is made), for four
  people.
- **4 from Keycloak.** One account creation per person.
- **16 from Directus.** Three task creations and one task update per person.
- **Ada's trace** comes from three systems that never talked to each other,
  yet reads as one process, in order. It even shows a step the tapped log
  could not: `hris:new applicant`, recorded by Odoo when the recruiter added
  her, before the coordinator was involved.

Two imported events, from `imported.ocel.json`:

```json
{ "id": "odoo-2", "type": "hris:stage First Interview", "time": "2026-11-02T18:05:07Z",
  "attributes": [ { "name": "source", "value": "odoo chatter" }, { "name": "record", "value": 120 } ],
  "relationships": [ { "objectId": "webapp:odoo", "qualifier": "target" },
                     { "objectId": "person:ada.byron+49c7b1@example.com", "qualifier": "about" } ] }

{ "id": "kc-22", "type": "identity:create:user", "time": "2026-11-02T18:05:07.430000Z",
  "attributes": [ { "name": "source", "value": "keycloak admin events" } ],
  "relationships": [ { "objectId": "webapp:keycloak", "qualifier": "target" },
                     { "objectId": "person:ada.byron+49c7b1@example.com", "qualifier": "about" } ] }
```

**The times here are real.** Imported events carry the time each system
recorded, unlike stage 1's steady clock. Odoo's chatter stamps to the whole
second, so the script spaces Alan's early account creation more than a second
before his hire. Otherwise the import could not tell their order.

---

## 6. Stage 3: Mine

**What happens.** Both logs are mined with **`person` as the case**: each
person is one run of the process. Repeated stage moves on one person are
folded into one step (`collapse_repeats`), so three interview stages and four
count the same.

**Which miner runs.** If R and the bupaR packages are installed, the example
uses **bupaR**, which also discovers a BPMN model. Otherwise it uses the
pure-Python **variant miner**, which finds the same activities, edges, and
variants, without a BPMN. The runner container has no R, so it always uses the
variant miner. Both give the same variants on these logs.

**Output** (with bupaR):

```
========================================================================
3. MINE — the process, its deviations, and the object-centric view
========================================================================
  from the observed calls — miner: bupaR · people: 4 · events: 24
  variant x3: call_operation:hr.recruitment.stage.search_read -> send_operation:hr.applicant.write
              -> send_operation:hr.applicant.create_employee_from_applicant -> send_operation:createItemsOnboardingTasks
              -> send_operation:post_users -> send_operation:updateSingleItemsOnboardingTasks
  variant x1: send_operation:post_users -> call_operation:hr.recruitment.stage.search_read
              -> send_operation:hr.applicant.write -> send_operation:hr.applicant.create_employee_from_applicant
              -> send_operation:createItemsOnboardingTasks -> send_operation:updateSingleItemsOnboardingTasks

  activities (how often each step happened):
      4  call_operation:hr.recruitment.stage.search_read
      4  send_operation:hr.applicant.write
      4  send_operation:hr.applicant.create_employee_from_applicant
      4  send_operation:createItemsOnboardingTasks
      4  send_operation:post_users
      4  send_operation:updateSingleItemsOnboardingTasks
  directly-follows (which step came straight after which):
      4  call_operation:hr.recruitment.stage.search_read -> send_operation:hr.applicant.write
      4  send_operation:hr.applicant.write -> send_operation:hr.applicant.create_employee_from_applicant
      4  send_operation:hr.applicant.create_employee_from_applicant -> send_operation:createItemsOnboardingTasks
      3  send_operation:createItemsOnboardingTasks -> send_operation:post_users
      1  send_operation:createItemsOnboardingTasks -> send_operation:updateSingleItemsOnboardingTasks
      1  send_operation:post_users -> call_operation:hr.recruitment.stage.search_read
      3  send_operation:post_users -> send_operation:updateSingleItemsOnboardingTasks
  BPMN: 6 nodes (6 tasks), 7 sequence flows
  -> hire_to_onboard_out/process_model.json

  from the systems' own histories — miner: bupaR · people: 4 · events: 33
  variant x3: hris:new applicant -> hris:stage First Interview -> hris:stage Contract Proposal
              -> hris:stage Contract Signed -> hris:note -> tracker:create:onboarding_tasks
              -> identity:create:user -> tracker:update:onboarding_tasks
  variant x1: hris:new applicant -> identity:create:user -> hris:stage First Interview
              -> hris:stage Second Interview -> hris:stage Contract Proposal -> hris:stage Contract Signed
              -> hris:note -> tracker:create:onboarding_tasks -> tracker:update:onboarding_tasks
  -> hire_to_onboard_out/imported_model.json

  handoff  webapp:odoo -> webapp:directus: 4 people
  handoff  webapp:directus -> webapp:keycloak: 3 people
  handoff  webapp:keycloak -> webapp:directus: 3 people
  handoff  webapp:keycloak -> webapp:odoo: 1 people
  object-centric view: {'person': 4, 'webapp': 3}; activities shared by people and systems: 9
```

(The variant lines are wrapped here; the script prints each on one line.)

**Reading it.**

- **Two variants, from the observed calls.** Three people followed the main
  process. One, Alan, followed a variant that **starts** with `post_users`:
  his account was created before anything else.
- **activities.** Each step happened 4 times, once per person.
- **directly-follows.** The edges show the deviation as numbers. The main path
  has `createItemsOnboardingTasks → post_users` 3 times, and Alan's path has
  `createItemsOnboardingTasks → updateSingleItemsOnboardingTasks` once,
  because his account already existed. `post_users → stage.search_read` (1)
  is his account coming first.
- **BPMN.** The discovered process as a BPMN model: six tasks, with the
  sequence flows between them.
- **Two variants, from the systems' own histories.** The imported log tells
  the same story in the systems' own words: three people get
  `identity:create:user` after `hris:stage Contract Signed`, and Alan gets it
  right after `hris:new applicant`. The two views were built independently and
  agree. It also shows Alan's extra "Second Interview".
- **Handoffs.** Every person moves Odoo → Directus. Three go Directus →
  Keycloak → Directus. Alan's path is the one `keycloak → odoo` handoff: he
  went to the identity server before the HR system.
- **Object-centric view.** `object_centric_dfg` builds one graph per object
  type, here people (4) and systems (3), instead of forcing everything onto one
  case type. Nine activities are shared between a person's view and a system's
  view: those are the points where the two meet. In a richer HR model (with
  requisitions, candidates, and employees as separate types), this is how you
  keep one-to-many relations, like one job and many candidates, without
  duplicating or dropping events.

### The miner's output files

**`process_model.json`** is what the miner found in the observed calls. Its
structure:

```json
{
  "miner": "bupaR",
  "n_cases": 4,
  "n_events": 24,
  "activities":       [ { "name": "send_operation:post_users", "frequency": 4 }, … ],
  "directly_follows": [ { "source": "send_operation:post_users",
                          "target": "send_operation:updateSingleItemsOnboardingTasks", "frequency": 3 }, … ],
  "variants": [
    { "sequence": [ "call_operation:hr.recruitment.stage.search_read", "send_operation:hr.applicant.write", … ],
      "frequency": 3 },
    { "sequence": [ "send_operation:post_users", "call_operation:hr.recruitment.stage.search_read", … ],
      "frequency": 1 }
  ],
  "bpmn": {
    "nodes": [ { "id": "send_operation:post_users", "name": "…", "type": "task" }, … ],
    "flows": [ { "source": "START", "target": "call_operation:hr.recruitment.stage.search_read" },
               …,
               { "source": "send_operation:updateSingleItemsOnboardingTasks", "target": "END_1" } ]
  }
}
```

**`imported_model.json`** has the same structure, for the imported history.
Its BPMN is larger (13 nodes, 19 flows in this run), because the imported
history has more steps and two paths to merge.

With the variant miner, `"miner"` is `"variants"` and `"bpmn"` is `null`.

**Where the files are:**

| How you ran it | Where they are |
|---|---|
| `./run.sh` (containers) | `out/hire_to_onboard/` next to `run.sh` |
| `uv run python examples/hire_to_onboard/run.py` | `hire_to_onboard_out/` in the AgenticHarvest root |
| with `--out DIR` | `DIR/` |

To load one in Python:

```python
from pathlib import Path
from agenticharvest.mining import ProcessModel

model = ProcessModel.model_validate_json(Path("out/hire_to_onboard/process_model.json").read_text())
for variant in model.variants:
    print(variant.frequency, variant.sequence)
```

---

## 7. Stage 4: Check the policy

**What happens.** The policy is written as **rules** and checked on every
person, over the observed log:

```python
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
```

The rule kinds:

| Kind | Means |
|---|---|
| `precedes` | If `then` happens, `first` happened before it. |
| `responds` | If `first` happens, `then` happens after it. |
| `within` | `then` happens no later than `max_hours` after `first`. |
| `exists` | `first` happens at least once. |
| `absent` | `first` never happens. |

An activity ending in `*` matches by prefix, e.g. `send_operation:post_*`.

After the check, the mined process is compiled into an agent plan.

**Output:**

```
========================================================================
4. POLICY — onboarding rules, checked on every person
========================================================================
  3/4 pass  account only after the hire
  4/4 pass  every hire gets an account
  4/4 pass  checklist opened within a day of the hire
  VIOLATION  alan.turing: account only after the hire — send_operation:post_users at 2026-10-05 17:00 with no send_operation:hr.applicant.create_employee_from_applicant before it

  compiled plan: 6 steps, 75% of observed hires, tools ['call_operation', 'send_operation']
```

**Reading it.**

- **3/4 pass.** Three people had their account created after their hire.
  Alan did not.
- **The violation** names the person, the rule, and what went wrong: his
  account (`post_users`) was created at 17:00 with no hire before it.
- **The other two rules pass for everyone,** Alan included. He did get an
  account, and his checklist was opened promptly.
- **compiled plan.** The process the agent will follow: the six steps of the
  main variant, which 3 of 4 (75%) of the observed hires followed exactly.
  Alan's path is **not** what the agent learns. The plan follows the dominant
  variant, which is also the compliant one.
- **Tools.** The agent gets `call_operation` (reads) and `send_operation`
  (writes). The other web tools (`list_resources`, `describe_operation`,
  `get_field_flow`) are not granted: the plan didn't use them.

The prompt the agent will receive is saved to `agent_prompt.txt`:

```
You onboard a newly hired person. Their applicant record is in the HR system (web app 'odoo').
Move the applicant to the 'Contract Signed' stage, then hire them with
hr.applicant.create_employee_from_applicant. Open their onboarding checklist in the tracker
(web app 'directus'): one row each with task 'laptop', 'account', and 'payroll', employee_ref
set to their work email, owner 'it' for laptop and account and 'finance' for payroll, and
status 'open 49c7b1'. Create their sign-in account in the identity server (web app 'keycloak',
realm 'agenticharvest'): username = the part of their email before '@', email = their work email,
firstName and lastName from their name, enabled true. Finally set the account task's status
to 'done 49c7b1'.

Follow this hire-to-onboard process. It was mined from 4 observed case(s); 75% of them
followed it exactly.

1. Call call_operation on 'hr.recruitment.stage.search_read'.
2. Call send_operation on 'hr.applicant.write'. This writes: pass confirm=true only once the checks above pass.
3. Call send_operation on 'hr.applicant.create_employee_from_applicant'. This writes: pass confirm=true only once the checks above pass.
4. Call send_operation on 'createItemsOnboardingTasks'. This writes: pass confirm=true only once the checks above pass.
5. Call send_operation on 'post_users'. This writes: pass confirm=true only once the checks above pass.
6. Call send_operation on 'updateSingleItemsOnboardingTasks'. This writes: pass confirm=true only once the checks above pass.

Use only what the tools return. If a step cannot be completed, stop and say which step failed
and why instead of improvising.
```

The first paragraph is the goal, written for this example: the details a
mined process can't know, such as the checklist items. The rest is generated
from the mined plan. Every write is marked.

Without `--agent`, the run ends here with:

```
Re-run with --agent to hand the plan to a live LLM (stage 5).
```

---

## 8. Stage 5: Agentify

Run with `--agent`.

**What happens.**

1. The recruiter (outside the process) opens a job and adds a new applicant,
   Katherine Johnson, whom the coordinator never saw.
2. A fresh, tapped registry is created, so the agent's own calls are recorded
   in their own log.
3. The agent is created with the stage-4 prompt and only the two granted
   tools, and asked: *"Onboard Katherine Johnson (work email …). Their
   applicant id in the HR system is 124. In every HR system call, include
   "context": {"email": "…"} in the body."*
4. The agent works through the tools on its own, against all three live
   systems. Its writes still go through the dry-run gate.
5. The script then checks the result **in the live systems**, its trace
   against the mined process, and the policy.

**Output:**

```
========================================================================
5. AGENTIFY — a live agent onboards a new hire
========================================================================
All done! Here's a full summary of everything completed for **Katherine Johnson**:

| Step | Action | Result |
|------|--------|--------|
| 1 | Found 'Contract Signed' stage | ID: 6 |
| 2 | Moved applicant 124 to 'Contract Signed' | ✅ Success |
| 3 | Created employee from applicant | Employee ID: 126 |
| 4a | Onboarding task: **laptop** (owner: it) | ID: 364, status: open 49c7b1 |
| 4b | Onboarding task: **account** (owner: it) | ID: 365, status: open 49c7b1 |
| 4c | Onboarding task: **payroll** (owner: finance) | ID: 366, status: open 49c7b1 |
| 5 | Created Keycloak account (`katherine.johnson+49c7b1`) | UUID: 7a50cbb6-... |
| 6 | Marked **account** task as done | ID: 365, status: done 49c7b1 |

  the agent's work, read back from the live systems:
    ok  hired in the HR system
    ok  one sign-in account
    ok  laptop, account, payroll tasks
    ok  account task done

  katherine.johnson: call_operation:hr.recruitment.stage.search_read -> send_operation:hr.applicant.write
                     -> send_operation:hr.applicant.create_employee_from_applicant
                     -> send_operation:createItemsOnboardingTasks -> send_operation:post_users
                     -> send_operation:updateSingleItemsOnboardingTasks
  conforms: True · fitness: 1.00
  policy: all rules pass
```

**Reading it.**

- **The table** is the agent's own summary. It is the LLM's text, so its exact
  wording varies between runs and models. The script does not trust it.
- **The `ok` lines** are the real check, read back from each system:

  | Check | Read from | Passes when |
  |---|---|---|
  | hired in the HR system | Odoo, the applicant record | stage is "Contract Signed" **and** an employee is linked |
  | one sign-in account | Keycloak, users with that email | exactly one exists |
  | laptop, account, payroll tasks | Directus, tasks with that `employee_ref` | exactly those three |
  | account task done | Directus, the account task | its status is `done <tag>` |

  A failed check prints `MISSING` instead of `ok`.
- **The trace** is the agent's own calls, mined. `check_trace` replays it
  against the model from stage 3. `conforms: True` and `fitness: 1.00` mean
  every step and every transition matched the observed process.
- **policy: all rules pass.** The same three rules from stage 4, checked on
  the agent's log. The agent hired Katherine before creating her account.

The script exits with code `0` when every check and the policy pass, and `1`
otherwise, so you can use it in CI.

### What the agent actually sent

Every raw call the agent made is saved to `agent_calls.json`, for review.
From this run:

```
ok  call_operation  odoo      hr.recruitment.stage.search_read
    {"domain": [["name", "=", "Contract Signed"]], "fields": ["id", "name"], "context": {"email": "katherine.johnson+…"}}
ok  send_operation  odoo      hr.applicant.write
    {"ids": [124], "vals": {"stage_id": 6}, "context": {"email": "…"}}, confirm: true
ok  send_operation  odoo      hr.applicant.create_employee_from_applicant
    {"ids": [124], "context": {"email": "…"}}, confirm: true
ok  send_operation  directus  createItemsOnboardingTasks
    [{"task": "laptop", "employee_ref": "…", "owner": "it", "status": "open 49c7b1"}, …], confirm: true
ok  send_operation  keycloak  post_users
    path_params {"realm": "agenticharvest"}, {"username": "katherine.johnson+49c7b1", "email": "…", "firstName": "Katherine", …}, confirm: true
ok  send_operation  directus  updateSingleItemsOnboardingTasks
    path_params {"id": 365}, {"status": "done 49c7b1"}, confirm: true
```

If the agent sends a malformed call, it shows up here with `ok: false` and the
error, and so does its retry. See [section 12](#12-what-the-safety-checks-do).

**If no LLM is configured,** stage 5 prints this and exits with code 1:

```
  no LLM endpoint configured (Missing LLM configuration: API_BASE, API_KEY, MODEL. …); skipping stage 5.
```

---

## 9. Cleanup

At the end of every run, success or failure:

```
  cleaned up: 5 accounts, 15 tasks, 5 employees, 5 applicants, 3 jobs
```

The run deletes everything carrying its tag from all three systems: the Odoo
applicants, employees, and job positions; the Keycloak accounts; and the
Directus tasks. Five people with `--agent` (four observed plus Katherine);
four without.

The audit trails in Odoo, Keycloak, and Directus keep their records of these
changes. That is what audit trails are for, and it doesn't affect later runs:
each run imports only its own people.

Pass `--keep` to skip the cleanup and inspect the records
([section 11](#11-look-at-it-yourself)).

## 10. The output files

Where they are:

- **Containers (`./run.sh`):** `out/hire_to_onboard/` next to `run.sh`. The
  runner writes them into a folder mounted from your machine, so they are
  ordinary files you can open. Set `AGENTICHARVEST_OUT` to choose another folder.
- **Python (`uv run …`):** `hire_to_onboard_out/` in the AgenticHarvest root.
- **Either, with `--out DIR`:** `DIR/`. (In containers, use `AGENTICHARVEST_OUT`
  instead.)

| File | Written by | What it holds |
|---|---|---|
| `observed.ocel.json` | stage 1 | The coordinator's tapped calls, as an OCEL 2.0 event log |
| `imported.ocel.json` | stage 2 | The three systems' own history, as an OCEL 2.0 event log |
| `process_model.json` | stage 3 | **The miner's output for the observed calls**: activities, directly-follows, variants, BPMN |
| `imported_model.json` | stage 3 | **The miner's output for the imported history** |
| `agent_prompt.txt` | stage 4 | The exact system prompt the agent received |
| `agent.ocel.json` | stage 5 | The agent's own tapped calls, as an event log |
| `agent_calls.json` | stage 5 | Every raw call the agent made: tool, parameters, success, error |

Both `.ocel.json` logs use the standard OCEL 2.0 JSON layout (`objectTypes`,
`eventTypes`, `events`, `objects`), so other process-mining tools can read
them.

## 11. Look at it yourself

Run with `--keep`, then open the apps in a browser:

| App | URL | Login | Where to look |
|---|---|---|---|
| Odoo | http://127.0.0.1:8069 | `admin` / `admin` | **Recruitment**: the job positions and applicants, each at "Contract Signed". Open an applicant to see its **chatter** at the bottom: the history stage 2 imported. **Employees**: the new hires. |
| Keycloak | http://127.0.0.1:8080/admin/ | `admin` / `agenticharvest-admin` | Switch the realm (top left) from `master` to **`agenticharvest`**. **Users**: the accounts. **Events → Admin events**: the history stage 2 imported. |
| Directus | http://127.0.0.1:8055/admin | `admin@example.com` / `agenticharvest-admin` | **Content → `onboarding_tasks`**: three tasks per person, with the account task `done`. **Activity** (bell icon): the history stage 2 imported. |

After a `--keep` run with `--agent`, you will find:

- **Odoo:** five applicants at "Contract Signed", each linked to an employee;
  job positions such as `Engineering (49c7b1)` and `Finance (49c7b1)`.
- **Keycloak:** five enabled users, e.g. `ada.byron+49c7b1`.
- **Directus:** fifteen tasks. For example:

  | id | employee_ref | task | owner | status |
  |---|---|---|---|---|
  | 352 | `ada.byron+49c7b1@example.com` | laptop | it | open 49c7b1 |
  | 353 | `ada.byron+49c7b1@example.com` | account | it | done 49c7b1 |
  | 354 | `ada.byron+49c7b1@example.com` | payroll | finance | open 49c7b1 |

You can also use the tools yourself, from the AgenticHarvest root:

```bash
uv run agenticharvest call call_operation app=odoo name=hr.applicant.search_read \
    'body={"domain": [], "fields": ["partner_name", "stage_id", "employee_id"]}'
uv run agenticharvest call call_operation app=keycloak name=get_users 'path_params={"realm": "agenticharvest"}'
```

To remove kept records later, delete them in each app's UI, or reset the whole
stack with `./run.sh reset`. That deletes all data in all three apps.

## 12. What the safety checks do

The agent works against three live systems, so the tools refuse anything unsafe
**before** a request is sent. These are real error messages:

**A malformed Odoo call.** Odoo answers a `write` that names `id` instead of
`ids` with success, and changes nothing. The tools check the body against
Odoo's own description of the method first:

```
Body for 'hr.applicant.write' is missing ids; unknown id. It takes: ids, vals, context.
```

The agent gets this as the tool result, corrects the call, and retries. Both
attempts show up in `agent_calls.json`.

**A write without approval** is blocked at the gateway:

```
Tool 'send_operation' has a write effect and requires 'confirm: true' to run.
```

**A write sent through the read tool** is refused, so a read-only grant really
is read-only:

```
'createItemsShipments' changes state (POST); use send_operation with confirm=true.
```

**Other checks**, on every call:

- A request whose URL leaves the app's address is refused.
- Each path parameter is quoted into a single URL segment, so a value cannot
  reach another endpoint.
- Credentials (the Odoo API key, the Keycloak admin login, the Directus token)
  are added when the request is sent. They never appear in a result, an event,
  `agent_calls.json`, or the logs.
- Keycloak's login token is refreshed automatically before it expires.
- Responses are capped at 20,000 characters, so a large listing cannot flood
  the agent.

## 13. Change it

**Different people.** Edit `COHORTS` and `NEW_HIRE` in `workflow.py`. Each entry
is `(name, email handle, role, stages before the hire)`. The stage names must
exist in Odoo's Recruitment: New, Qualification, First Interview, Second
Interview, Contract Proposal, Contract Signed.

**No violation.** Set `EARLY_ACCOUNT = ""` in `workflow.py`. Stage 3 then mines a
single variant, and stage 4 reports `4/4 pass` for every rule.

**A different policy.** Edit `POLICY` in `workflow.py`, using the rule kinds in
[stage 4](#7-stage-4-check-the-policy). For example, to require the
checklist within an hour:

```python
Rule(name="checklist within an hour", kind="within",
     first="send_operation:hr.applicant.create_employee_from_applicant",
     then="send_operation:createItemsOnboardingTasks", max_hours=1)
```

**Mining your own HR estate.**

1. **Configure each system** as a web app. See "Pointing AgenticHarvest at your own
   systems" in the [agenticharvest-systems README](https://github.com/aster-test-generation/agenticharvest-systems#pointing-agenticharvest-at-your-own-systems).
   Use a sandbox tenant.
2. **Find each system's history.** Most HR, identity, and ticketing products
   keep an audit trail with an API. Write one `RecordMapping` per source.
3. **Pick the key.** The work email is a good start. An employee number works
   if every system stores it.
4. **Import, mine, check.** `import_records`, then `mine`,
   `object_centric_dfg`, and `check_rules` with your policy. No one has to
   change how they work, and no tapping is needed.

## 14. Troubleshooting

| You see | What it means | Fix |
|---|---|---|
| `A live system is not configured: Web app 'odoo' is not configured …` | No `.env` with the apps' settings | Run `setup.py` (or use `run.sh`). Run host scripts from the AgenticHarvest root. |
| `… Connection refused` | An app isn't running | `podman compose -f examples/systems/compose.yaml up -d` |
| `odoo … is not answering` | Odoo is still initializing (first start) | Wait a minute or two, then run again. |
| `0 events from keycloak admin events` | Admin events are off for the realm | Run `setup.py`; it turns them on. |
| `VIOLATION` for a person other than Alan | A step failed partway | Read stage 1's output for errors, or rerun with `--keep` and look in the apps. |
| `MISSING hired in the HR system` | The agent didn't complete the hire | Check `agent_calls.json` for the failing call and its error. |
| `conforms: False` | The agent took a step outside the mined process (e.g. an extra read) | Its work may still be correct: see the `ok` lines. The trace shows the extra step. |
| `no LLM endpoint configured` | `--agent` without `API_BASE` / `API_KEY` / `MODEL` | Set them in the environment or `.env`. |

For problems starting the stack itself, see the troubleshooting table in the
[agenticharvest-systems README](https://github.com/aster-test-generation/agenticharvest-systems#troubleshooting).

## 15. How the code is organized

| File | What it holds |
|---|---|
| `workflow.py` | The correlation rules (`KEY_RULES`), the people (`COHORTS`, `NEW_HIRE`), the coordinator's process (`onboard_by_hand`), the history mappings (`ODOO_HISTORY`, `KEYCLOAK_HISTORY`, `DIRECTUS_HISTORY`) and their readers, the policy (`POLICY`), mining, and cleanup |
| `run.py` | The five stages, in order, printing what you see above |
| `run.sh`, `run.ps1` | The containers-only launchers |

Everything specific to this example lives in these files. The pieces they use
from AgenticHarvest are generic:

| From AgenticHarvest | Used for |
|---|---|
| `agenticharvest.domains.web` | The tools for all three apps, built from their API descriptions, and the event mapper |
| `agenticharvest.core.events`: `CompositeMapper`, `Correlator`, `KeyRule`, `OcelTap` | One log across three systems, keyed by person |
| `agenticharvest.core.events`: `RecordMapping`, `ObjectRef`, `import_records` | Stage 2 |
| `agenticharvest.mining`: `BupaRMiner`, `VariantMiner`, `collapse_repeats`, `check_trace` | Mining and conformance |
| `agenticharvest.mining.analysis`: `handoffs`, `object_centric_dfg`, `check_rules`, `Rule` | Stages 3 and 4 |
| `agenticharvest.orchestration`: `compile_plan`, `restrict_tools` | Stages 4 and 5 |

AgenticHarvest's test `tests/examples/test_cross_system.py` runs stages 1 to 4
against the live systems and asserts the results shown on this page:
[test_cross_system.py](https://github.com/IBM/agenticharvest/blob/main/tests/examples/test_cross_system.py).
