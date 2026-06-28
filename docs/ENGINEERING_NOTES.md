# Engineering Notes

## Why this exists at all

I didn't start this project because agents hallucinate. Everyone already knows that problem exists. What interested me was a different failure mode: an agent making a perfectly valid tool call that was still the wrong action. Existing tooling mostly explains these failures after the fact. I wanted to see whether runtime policy enforcement could be implemented as a small Python primitive instead.

The instinct early on was "wrap the tool call, check it against rules, that's it." That's basically still what shipped. The temptation the whole way through was to make it bigger than that.

---

## Scope: the fight against becoming an observability platform

After the MVP started taking shape, the idea came up to expand AgentGuard into something that tracks full agent activity, runs evaluations across models, has a dashboard, scores accuracy, etc. basically LangSmith. On paper a lot of that sounded reasonable. Some of it I'd even want as a user.

Several ideas sounded genuinely useful—dashboards, evaluation reports, prompt tracing, LLM-generated explanations. I almost convinced myself to build them.

I eventually reduced every feature request to one question:
Does this strengthen runtime governance?
If the answer was no, I left it out.

What actually survived from that conversation: a governance-enhancements addendum (policy simulator, policy testing framework, governance reports, risk scoring, multi-step approvals) that stays inside the "deterministic policy + audit trail" boundary. None of it is built yet. What ended up mattering more was "reject all new features" it was "every new feature has to be explainable using only data the audit log already produces, no new telemetry, no model judgment calls." That's a real filter, and it's the one thing from that whole detour worth keeping.

If someone asks for a feature later that needs full prompt logging or an LLM-generated explanation of agent behavior that's the line. Push back the same way.

---

## SQLite, not anything else

SQLite fit the project constraints almost immediately: zero setup, transactional writes, and a local-first workflow. I never found a compelling reason to introduce another dependency during the MVP. SQLite gives transactional writes, a real schema, and zero ops burden. The trade-off is no real concurrent-writer story beyond WAL mode, but for what this does (governance decisions, not high-throughput event logging), that's fine.

What I didn't anticipate going in: **how much of the actual engineering effort would go into making SQLite access safe**, not into the governance logic itself. The policy engine, budget tracker, loop detector those came together fast and didn't really fight back. The storage layer is where the real bugs lived.

### The connection-per-thread thing

Early on I had to decide: one shared connection, a connection pool, or one connection per thread. Went with thread-local connections (`threading.local()`), `check_same_thread=True` on purpose not `False` as a shortcut. The reasoning: I wanted a wrong cross-thread use to *fail loudly* (raise immediately) instead of silently corrupting data. `check_same_thread=False` is the "I'll just turn off the safety check" move and it doesn't actually buy you anything; you still need real synchronization, you've just hidden the symptom.

I think this turned out to be the right choice. Later concurrency tests (100-thread budget updates, approval races) validated the approach without requiring me to relax SQLite's safety guarantees.

### The WAL mode surprise

First time testing this on Windows (not in the sandbox on the actual laptop this was being built for), running a one-line `python -c "..."` to create the DB and check tables came back empty. Confusing for a minute the file existed, `Tables created OK` had printed, but querying it showed nothing.

Turned out: WAL mode keeps writes in a separate `-wal` file until a clean checkpoint, which mostly happens when the connection actually gets closed. A one-off Python `-c` command that constructs a `DatabaseManager` and exits without calling `close_thread_connection()` doesn't guarantee that checkpoint happens before the process dies. Lesson, which now lives as policy: **always close the connection explicitly**, don't rely on garbage collection or process exit to flush WAL. This is exactly the kind of thing that looks like a bug in the code but is actually a bug in not understanding the storage engine's commit semantics.

### The `id()` test bug

Wrote a test asserting two threads get different SQLite connection objects, using `id(conn1) != id(conn2)`. Failed on Windows, passed in the Linux sandbox. Not a real concurrency bug a test methodology bug. Once a thread finishes and its connection object goes out of scope, the memory can get reused by the next object created. Comparing `id()` of two objects that don't coexist in memory at the same time is just comparing recycled addresses, not object identity. Fixed by keeping live references to both connections until the comparison happens. Embarrassing in hindsight `id()` is literally "memory address," and I momentarily forgot that means nothing once the object's dead.

This is One thing I didn't expect: a test passing on one platform and failing on another isn't always a real platform difference in the code. Sometimes it's the test that's wrong, and the platform difference is just allocator behavior making the wrongness visible.

---

## The policy-reload race the one I'd fix first if I had to do it again

The original `DecisionEngine.evaluate()` called `policy_engine.evaluate(ctx)` to get the rule match, then separately read `policy_engine.policy.budget` afterward to get budget limits. Each read individually grabbed `PolicyEngine`'s internal lock and was atomic *by itself*. But the two reads together weren't atomic as a pair. If `reload()` landed on another thread in between them, you could end up with a decision where the matched rule came from policy version N and the budget threshold came from version N+1. Same call, two different policy versions silently blended into one decision.

The dangerous part was that nothing crashed.
Everything looked valid.
The decision was simply wrong.

The fix is small (capture one snapshot at the top of `evaluate()`, use it for everything in that call) but finding it wasn't a test failure it was a structured "trace through every component, ask where two states could disagree" review. 
The lesson: code review for concurrency bugs needs to specifically ask "where do I read shared state more than once in one logical operation," because individually-atomic reads don't compose into an atomic operation just because each one is safe alone.


---

## Approvals: the contradiction that was already in the spec before any code existed

The original design doc, before implementation started, had a real self-contradiction: the error-handling table said "last writer wins" for concurrent approval updates, and the open-questions section recommended "first writer wins" for the exact same scenario. Nobody had reconciled it.

I ended up choosing first-writer-wins.

I can imagine systems where last-writer-wins is reasonable, but for human approvals it felt wrong. The first recorded human decision should be the one preserved in the audit trail.

Implementation: `UPDATE approvals SET status=... WHERE approval_id=? AND status='PENDING'`. If the row's no longer PENDING by the time this runs, the guard clause means zero rows get touched, and the caller gets back `False` rather than silently overwriting. Simple once you see it. The harder part was making sure this was actually tested under real threads racing each other, not just two sequential calls sequential calls would never have caught a real race even if the logic were wrong, since there's no actual concurrency happening.

Same pattern got reused for lazy expiration (a `PENDING` approval might get found "expired" by one thread at the exact moment another thread is approving it) same guarded-UPDATE trick closes that too.

---

## The `sqlite3.IntegrityError` mistake, made twice

Built `ApprovalManager.create_approval()`. Tested it. Found that calling it twice with the same composite key let a raw `sqlite3.IntegrityError` propagate straight out of the `UNIQUE` constraint, instead of AgentGuard's own exception type. Fixed it wrapped it, re-raised as `AgentGuardDatabaseError` with a message telling the caller to use `find_existing()` first.

Then, later, built `AuditLogger.log_decision()`. Same exact mistake. A dangling `approval_id` foreign-key reference raised the same raw `sqlite3.IntegrityError`, unwrapped, straight through the public API.

What surprised me wasn't the first bug. It was making the same mistake again in a different subsystem only a few days later.

---

## The audit log lying about what actually happened biggest behavioral fix in the project

This is the one I'd flag as the most consequential correction, and it came from running an actual end-to-end smoke test, not from a unit test.

The flow: a tool requires approval. First call raises `ApprovalRequiredException`, logs a `REQUIRE_APPROVAL` audit row correct, that's genuinely what happened. Human approves it. Agent retries the same call. It goes through, the real function runs, result comes back.

First version of this logged that second event as `REQUIRE_APPROVAL` too because that's what the Policy Engine's rule said the tool's classification *is*. Technically the policy classification didn't change. But the actual *outcome* was that the tool executed. So now you've got an audit log where a `count_by_decision()` query reports inflated `REQUIRE_APPROVAL` numbers that include calls that were actually allowed through, and there's no column distinguishing "still pending" from "was approved and ran." If anyone built a report off this (which the addendum's governance-reports feature explicitly plans to), it would be quietly wrong.

The fix: log the *true outcome* (`ALLOW` or `DENY`) for the resolution event, while leaving the original `REQUIRE_APPROVAL` row from the initial request untouched it's a true record of what the state was at that earlier moment, just not the final outcome. Same fix applies symmetrically on the rejection path.

The thing I learned here: a decision label and an execution outcome are two different things, and conflating them is an easy mistake to make because most of the time they're the same thing (ALLOW means it ran, DENY means it didn't). The one case where they diverge approval that gets resolved later is exactly the case the whole approval feature exists for. If I'd only checked this with unit tests in isolation rather than running the actual end-to-end flow and reading the resulting log by eye, I'm not sure this gets caught before someone hits it in practice.

---

## The idempotency bug that depended on whether you used keyword args

This one's a good example of "the fix order matters, and I had it backwards."

`@guard.tool`'s wrapper needs to bind the call's arguments into a dict (for the idempotency hash) and also needs to strip out `run_id` before calling the real wrapped function (since the real function's signature doesn't have a `run_id` parameter). First version did signature binding *first*, then stripped `run_id` afterward.

Problem: `inspect.signature(fn).bind(*args, **kwargs)` with `run_id` still sitting in `kwargs` almost always raises `TypeError`, because the real function's signature genuinely doesn't accept `run_id`. That's not an edge case that's the *normal* call, since passing `run_id` as a kwarg is the documented way to call these wrapped functions. So the binding call failed nearly every time, and fell into a fallback path that didn't bind cleanly against the signature it just dumped whatever args/kwargs it got into a dict more directly. Positional calls and keyword calls ended up producing differently-shaped dicts for what was logically the exact same call, which meant they hashed differently, which meant the idempotency guarantee (same call → same approval, no duplicates) silently broke depending on how the caller happened to invoke the function.

Caught this by literally testing the thing the docs claimed worked called the same logical operation once positionally and once with keywords and checked whether the resulting `approval_id` matched. It didn't. Fix was just reordering: strip `run_id` first, bind signature against the clean args after. Obvious in hindsight, the kind of bug that's invisible if you only test one calling convention.

---

## Same-tool-name test trap, three times

Lost more time to this than I'd like to admit. Pattern: write a test (or demo) that calls the same tool name repeatedly to check budget exhaustion except loop detection *also* triggers on repeated calls to the same tool, and if both thresholds happen to be set to similar numbers, loop detection fires first and the test passes for the wrong reason, or fails in a way that looks like the wrong subsystem is broken.

Hit this in the decision-engine tests. Hit it again in a `core.py` test for `reset_run`. Hit it a third time in the actual demo script the budget-exhaustion demo was silently demonstrating loop detection instead, because both were configured to trip at 3 calls and the call pattern used one tool repeatedly.

The fix is always the same: use distinct tool names when you want to isolate budget behavior from loop behavior. I should have internalized this after the first occurrence. I made this mistake three separate times. That was enough to convince me to document it.

Interestingly fixing the demo this way (four different tools instead of one tool four times) ended up demonstrating something true and worth knowing anyway budget in this system is a single counter shared across every tool in a run, not a per-tool allowance. Wasn't trying to showcase that, but the bug fix accidentally produced a better demo.

---

## Wrapper vs. building a new runtime

Never seriously considered building AgentGuard as its own execution runtime or framework. The decision to wrap existing callables (`@guard.tool` / `.wrap()`) instead of requiring agents to run inside some AgentGuard-specific execution model was there from the start and never wavered. Reasoning: the moment you require people to adopt your runtime, you've made adoption a much bigger ask, and you've also taken on the job of replicating whatever the host framework already does (state management, retries, orchestration) which is explicitly not what this is supposed to be.

The harder design question wasn't wrapper-vs-runtime, it was wrapper-vs-decorator i.e., do you hand someone a function that wraps their tool (`guard.wrap(fn)`) or do you give them a decorator (`@guard.tool`) to put on their own function. Ended up building both, with the decorator as primary. The decorator's nicer for code you control; the wrapper exists because sometimes you're wrapping a third-party function you can't put a decorator on directly. Not a hard call just worth having both rather than picking one and making someone work around it.

I briefly considered building a dedicated runtime because it would make enforcement simpler internally.

I rejected it because adoption matters more than architectural purity. Wrapping existing tools means people can try AgentGuard without rewriting their agents.
---

## What the zero-cost constraint actually changed

The decision to keep this project at zero API cost (no LLM calls anywhere, no cloud service required) wasn't just a budget constraint it shaped real architecture decisions:

- It's a big part of why "explicit YAML policies, no LLM judgment" was non-negotiable from day one, not just a nice principle. An LLM-based policy judge would have meant either real API cost or installing a local model, neither of which fit.
- It's why the LangGraph adapter got deliberately deferred rather than built alongside everything else not because it's hard, but because pulling in ~107MB of dependencies just to write an adapter felt wasteful before the core governance logic was even fully proven. Decided to install it only once actually doing that specific work, not speculatively.
- It's why the OpenAI Agents SDK adapter doesn't even have a planned "install it for real" step yet the plan there is to build against the documented interface and accept slightly more risk of drift from the real package, rather than install a second large dependency tree for a feature that isn't being built yet anyway.

Looking back, the zero-cost constraint simplified more decisions than it complicated. It kept the architecture local, deterministic, and focused on governance rather than infrastructure.

---

## Things I'd still flag as open, not resolved

I'm leaving these here because rather than letting them quietly age into "must be fine since nobody complained":

- `BudgetExceededException` and `LoopDetectedException` exist in the exception hierarchy but are never actually raised both surface as the more generic `ToolDeniedException`. Built the specific exception types early, then never wired them in once the actual override logic landed in `DecisionEngine`. Not urgent, but it's a real gap between what the exception hierarchy implies and what callers can actually catch.
- Async tool calls don't dispatch the SQLite I/O through `asyncio.to_thread()`. The governance check is synchronous DB work happening inside an `async def` wrapper, which means it blocks the event loop for however long that I/O takes. Hasn't caused a problem in testing because the test database is fast and local, but it's exactly the kind of thing that's invisible until someone's running this under real async load.
- `reload_policy()` re-reads the policy file but doesn't reconstruct the `LoopDetector` if the reloaded policy changes loop thresholds `LoopDetector` gets sized once, at construction. A live policy change to loop settings won't actually take effect until the process restarts. Noted directly in the code's own docstring rather than left silent, but not fixed.

I'm deliberately leaving these unresolved instead of pretending the MVP is perfect.

---

## What I'd actually keep, if rebuilding this from scratch

- Thread-local SQLite connections with `check_same_thread=True`. Worth it.
- Capturing exactly one snapshot of mutable shared state per logical operation, never re-reading it mid-operation. The reload-race bug is the canonical example of why.
- Testing real concurrency with real threads, not just sequential calls dressed up as "concurrency tests." Every meaningful bug in the approval/budget layer only showed up under actual thread contention.
- Treating "does the documented entry point actually work" as its own explicit check, separate from "do the unit tests pass."
- The discipline from the scope fight: every feature has to be explainable from data the system already produces, no new telemetry, no model judgment calls. That one rule did more to keep this project coherent than any architecture diagram did.

---

## If I were starting AgentGuard again today:

- I'd still choose SQLite.
- I'd still keep deterministic YAML policies.
- I'd still wrap existing frameworks instead of building a runtime.

The biggest thing I'd change is introducing the adapter layer earlier. Most of the governance architecture stabilized quickly; validating it against a real framework would probably have uncovered integration issues sooner.

The other thing I'd do earlier is write more end-to-end tests. Most of the interesting bugs weren't inside individual components—they appeared only when several pieces interacted.

One thing I noticed while building AgentGuard is that most of the difficult problems weren't algorithmic.

They were about defining correct behavior at the boundaries between components.

Most of the bugs that mattered came from interactions between otherwise-correct components: policy reloads, approval resumption, audit logging, and concurrency.

That's probably the biggest lesson I'll carry into future infrastructure work.

