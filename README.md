# Hire to onboard: mining an HR estate

> **[WALKTHROUGH.md](WALKTHROUGH.md)** goes through every stage in detail: what
> runs, the full output with an explanation of each line, the event logs and
> the miner's output files, where they land, how to look at the results in
> Odoo, Keycloak, and Directus, and how to change the example.

Hiring someone touches three live systems, and each one holds its own part of
the record:

| System | Product | What it holds | The person's id there |
|---|---|---|---|
| HR system | Odoo 19 (Employees + Recruitment) | job positions, applicants, employees | applicant id, then employee id |
| Identity server | Keycloak 26 | the account they sign in with | a user UUID |
| Task tracker | Directus 11 | the onboarding checklist | three task ids |

No id appears in more than one system. This example mines the process
anyway, in two ways:

- **Observed.** The HR coordinator's work goes through AgenticHarvest's tools and is
  tapped live.
- **Imported.** The same process is rebuilt from each system's own history
  (Odoo's chatter, Keycloak's admin events, Directus's activity feed), with
  no tapping at all. This is how you mine a system nobody routes through
  AgenticHarvest.

It then checks the business's onboarding policy on every person, and has an
agent onboard a new hire by the mined plan.

## Run it

**With nothing installed but Podman or Docker** (and git). Python, AgenticHarvest,
and all three apps run in containers:

```bash
git clone https://github.com/aster-test-generation/agenticharvest-example-hire-to-onboard.git
cd agenticharvest-example-hire-to-onboard
./run.sh                 # stages 1-4      (Windows: .\run.ps1)
./run.sh --agent         # + stage 5; export API_BASE, API_KEY, MODEL first
./run.sh --keep          # leave the records in the apps, to look at them
```

On first use, `run.sh` fetches AgenticHarvest (with this example and the live stack
as its submodules) into `./.agenticharvest`. Inside an AgenticHarvest checkout, it uses that
checkout instead. Cloned on its own, `run.sh` runs the copy of this example
inside `.agenticharvest`, so edits to the files in this clone are not used. To
change the example, work in an AgenticHarvest checkout
(`git clone --recurse-submodules https://github.com/IBM/agenticharvest.git`)
and edit `examples/hire_to_onboard/`. The first run pulls about 2 GB of images and takes a few
minutes. [`agenticharvest-systems`](https://github.com/aster-test-generation/agenticharvest-systems#readme) explains what starts, how to
open the apps in a browser, and how to fix common problems.

**With Python and uv**, from the AgenticHarvest repo root:

```bash
podman compose -f examples/systems/compose.yaml up -d
uv run python examples/systems/setup.py

uv run python examples/hire_to_onboard/run.py            # stages 1-4
uv run python examples/hire_to_onboard/run.py --agent    # + stage 5, live LLM
```

Every person a run creates has a tagged work email
(`ada.byron+3f9a2c@example.com`). When the run finishes, it removes everything
carrying that tag from all three systems: applicants, employees, job
positions, accounts and tasks. Pass `--keep` to leave them for inspection.

## The coordinator's process

Each step is one tool call, and one case per **person**:

```
0. look up the recruitment stages     call_operation:hr.recruitment.stage.search_read          HR
1. move the applicant through them    send_operation:hr.applicant.write                         HR
2. hire: Odoo creates the employee    send_operation:hr.applicant.create_employee_from_applicant HR
3. open the onboarding checklist      send_operation:createItemsOnboardingTasks                 tracker
4. create their sign-in account       send_operation:post_users                                 identity
5. close the account task             send_operation:updateSingleItemsOnboardingTasks           tracker
```

The case key is the one value every system carries: the **work email**. The
correlator finds it in Odoo call bodies, in Keycloak's user body and in the
tracker's `employee_ref`.

Odoo identifies records only by id, so the coordinator passes the email in the
JSON-2 call's `context`. Odoo ignores context keys it doesn't use, and this
puts every HR call on the person.

One of the four observed hires, Alan, has his account created **before** his
contract is signed. This is the deviation the policy check should catch.

## The five stages

### 1. Observe

```
  ada.byron     applicant 7, employee 9, tasks [13, 14, 15]
  …
  33 tool calls -> hire_to_onboard_out/observed.ocel.json
```

### 2. Import: the same process from the systems' own records

Each source is turned into events by a declarative `RecordMapping` in this
example's `workflow.py`; AgenticHarvest's importer (`import_records`) knows
nothing about these products:

```python
KEYCLOAK_HISTORY = RecordMapping(
    time="time", time_format="epoch-ms",
    activity="identity:{operationType}:{resourceType}", lowercase_activity=True,
    objects=[ObjectRef(type="webapp", value="keycloak", constant=True),
             ObjectRef(type="person", path="representation.email")],
)
```

```
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

The imported history shows things the tapped log can't. For example, Odoo
recorded the "new applicant" event, which happened before the coordinator
became involved.

### 3. Mine

Both logs are mined, the observed one and the imported one. Each prints its
variants, activity counts, and directly-follows edges, plus a BPMN when R and
bupaR are installed. Each model is saved as JSON: `process_model.json` and
`imported_model.json` in the output folder.

```
from the observed calls — miner: bupaR · people: 4 · events: 24
variant x3: …stage.search_read -> …applicant.write -> …create_employee_from_applicant
            -> createItemsOnboardingTasks -> post_users -> updateSingleItemsOnboardingTasks
variant x1: post_users -> …stage.search_read -> …applicant.write -> …
BPMN: 6 nodes (6 tasks), 7 sequence flows
-> hire_to_onboard_out/process_model.json

from the systems' own histories — miner: bupaR · people: 4 · events: 33
variant x3: hris:new applicant -> … -> hris:stage Contract Signed -> … -> identity:create:user -> …
variant x1: hris:new applicant -> identity:create:user -> hris:stage First Interview -> …
-> hire_to_onboard_out/imported_model.json

handoff  webapp:odoo -> webapp:directus: 4 people
object-centric view: {'person': 4, 'webapp': 3}
```

The two views were built independently and agree: in both, Alan's account
comes first. [WALKTHROUGH.md](WALKTHROUGH.md#6-stage-3-mine) shows the full
output and the model files.

`object_centric_dfg` keeps a directly-follows graph per object type (people
and systems here; requisitions, candidates and employees in a richer HR
model). It doesn't force everything onto one case type, which would duplicate
or drop events when objects relate one-to-many.

### 4. Policy

```python
POLICY = [
    Rule(name="account only after the hire", kind="precedes",
         first="send_operation:hr.applicant.create_employee_from_applicant",
         then="send_operation:post_users"),
    Rule(name="every hire gets an account", kind="exists", first="send_operation:post_users"),
    Rule(name="checklist opened within a day of the hire", kind="within",
         first="send_operation:hr.applicant.create_employee_from_applicant",
         then="send_operation:createItemsOnboardingTasks", max_hours=24),
]
```

```
  3/4 pass  account only after the hire
  4/4 pass  every hire gets an account
  4/4 pass  checklist opened within a day of the hire
  VIOLATION  alan.turing: account only after the hire — send_operation:post_users at … with no …create_employee_from_applicant before it
```

The imported history agrees. In Alan's record from the three systems,
`identity:create:user` comes before `hris:stage Contract Signed`. The live
test checks both views.

### 5. Agentify

The agent onboards a new hire using the compiled plan and the policy. The
script then reads the work back from all three systems:

```
  the agent's work, read back from the live systems:
    ok  hired in the HR system
    ok  one sign-in account
    ok  laptop, account, payroll tasks
    ok  account task done

  katherine.johnson: …stage.search_read -> …applicant.write -> …create_employee_from_applicant
                     -> createItemsOnboardingTasks -> post_users -> updateSingleItemsOnboardingTasks
  conforms: True · fitness: 1.00
  policy: all rules pass
```

The agent's raw calls are saved to `agent_calls.json` for review. If a call
had the wrong shape (for example, an Odoo `write` sent with `id` instead of
`ids`), the web tools reject it with the parameters the operation actually
takes. Without that check, Odoo would accept the call and silently do nothing.

## Files

| File | What it holds |
|---|---|
| `workflow.py` | The correlation rules, the coordinator's process, the history mappings, the policy, cleanup |
| `run.py` | The five stages, printed |

AgenticHarvest's [`tests/examples/test_cross_system.py`](https://github.com/IBM/agenticharvest/blob/main/tests/examples/test_cross_system.py)
runs stages 1–4 against the live systems.
[`test_cross_system_agents.py`](https://github.com/IBM/agenticharvest/blob/main/tests/examples/test_cross_system_agents.py)
runs stage 5 (`pytest -m real_llm`).

## Mining your own HR estate

1. **Configure each system** as a web app (see
   [agenticharvest-systems](https://github.com/aster-test-generation/agenticharvest-systems#pointing-agenticharvest-at-your-own-systems)).
   Use a sandbox tenant.
2. **Find its history.** Most HR, identity and ticketing products keep an
   audit trail with an API. Write one `RecordMapping` per source: where the
   time is, how to name the activity, and which field names the person.
3. **Pick the key.** The work email is a good start. An employee number works
   if every system stores it.
4. **Import, mine, check.** Run `import_records`, then `object_centric_dfg` and
   `check_rules` with your policy. No one has to change how they work.
