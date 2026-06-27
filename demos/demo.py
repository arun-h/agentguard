#!/usr/bin/env python3
"""
AgentGuard Demo

Run with:
    pip install agentguard
    python demo.py

This script exercises the three core governance scenarios from the
Engineering Design Specification (Section 11):

    1. Wire Transfer Approval Flow  -- a tool requiring human approval
       is blocked, then resumes correctly after approval is granted.
    2. Loop Detection                -- an agent stuck calling the same
       tool repeatedly is detected and blocked.
    3. Budget Exhaustion             -- a run that exceeds its configured
       tool-call budget is denied on the call that crosses the limit.

Everything here uses the REAL agentguard package -- no governance logic
is reimplemented in this script. Tools are mock functions that print to
the console; no real payment, database, or email service is called, and
no LLM or external API is used anywhere in this demo (zero cost, zero
API key required).

A real SQLite database file (demo.db) is created in the same directory
as this script, containing the full audit trail of every decision made
during the run.
"""
from __future__ import annotations

import os
import sys
import time

# Ensure the project root (the directory containing the `agentguard`
# package) is importable regardless of how this script is invoked --
# e.g. `python demo.py` from inside demos/, or `python demos/demo.py`
# from the project root. Without this, running the script directly by
# path sets sys.path[0] to the demos/ directory itself (which has no
# agentguard package inside it), which can resolve `agentguard` as a
# broken/empty namespace package via the editable install's path hook
# instead of either cleanly finding or cleanly failing to find it.
_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_DEMO_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agentguard import AgentGuard, ApprovalRequiredException, ToolDeniedException

DEMO_DIR = _DEMO_DIR
POLICY_PATH = os.path.join(DEMO_DIR, "demo_policy.yaml")
DB_PATH = os.path.join(DEMO_DIR, "demo.db")


def banner(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def step(text: str) -> None:
    print(f"\n>>> {text}")


def result(text: str) -> None:
    print(f"    {text}")


def main() -> None:
    # Start from a clean database each time the demo runs, so the
    # output is reproducible and not affected by a previous run's state.
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for suffix in ("-wal", "-shm"):
        leftover = DB_PATH + suffix
        if os.path.exists(leftover):
            os.remove(leftover)

    guard = AgentGuard.from_policy(POLICY_PATH, db_path=DB_PATH)

    # ------------------------------------------------------------------
    # Mock tools. These represent what a real agent's tools would look
    # like -- AgentGuard intercepts calls to these exactly the same way
    # it would intercept calls to a real payment API, database, or email
    # service. None of these tools call any real external system.
    # ------------------------------------------------------------------

    @guard.tool
    def wire_transfer(account_id: str, amount: float) -> dict:
        print(f"    [TOOL EXECUTED] Wire transfer: ${amount:,.2f} -> {account_id}")
        return {"status": "transferred", "account_id": account_id, "amount": amount}

    @guard.tool
    def delete_customer(customer_id: str) -> dict:
        print(f"    [TOOL EXECUTED] Deleted customer: {customer_id}")
        return {"status": "deleted", "customer_id": customer_id}

    @guard.tool
    def search_database(query: str) -> list:
        print(f"    [TOOL EXECUTED] Searching database for: {query!r}")
        return [f"result-for-{query}"]

    @guard.tool
    def fetch_data(source: str) -> dict:
        print(f"    [TOOL EXECUTED] Fetching data from: {source}")
        return {"source": source, "rows": 42}

    @guard.tool
    def fetch_orders(source: str) -> dict:
        print(f"    [TOOL EXECUTED] Fetching data from: {source}")
        return {"source": source, "rows": 17}

    @guard.tool
    def fetch_inventory(source: str) -> dict:
        print(f"    [TOOL EXECUTED] Fetching data from: {source}")
        return {"source": source, "rows": 88}

    @guard.tool
    def fetch_shipments(source: str) -> dict:
        print(f"    [TOOL EXECUTED] Fetching data from: {source}")
        return {"source": source, "rows": 5}

    # ==================================================================
    # DEMO 1: Wire Transfer Approval Flow
    # ==================================================================
    banner("DEMO 1: Wire Transfer Approval Flow")
    run_id = "demo-run-001"

    step("Agent attempts to wire transfer $5,000.00 to account ACC123")
    try:
        wire_transfer("ACC123", 5000.00, run_id=run_id)
        result("UNEXPECTED: transfer should have required approval")
    except ApprovalRequiredException as e:
        approval_id = e.approval_id
        result(f"BLOCKED. Approval required.")
        result(f"Approval ID: {approval_id}")
        result(f"Reason: {e.reason}")

    step("Checking pending approvals...")
    pending = guard.get_pending_approvals()
    result(f"{len(pending)} pending approval(s):")
    for p in pending:
        result(f"  -> {p.approval_id} | {p.tool_name} | {p.status.value}")

    step(f"Human approver 'alice' approves {approval_id}")
    guard.approve(approval_id, approver="alice", notes="Verified with customer")
    result("Approved.")

    step("Agent retries the same wire transfer call (resuming after approval)")
    response = wire_transfer("ACC123", 5000.00, run_id=run_id)
    result(f"Transfer completed: {response}")

    step("Demonstrating a permanently denied tool: delete_customer")
    try:
        delete_customer("CUST456", run_id=run_id)
        result("UNEXPECTED: delete should have been denied")
    except ToolDeniedException as e:
        result(f"DENIED outright. Reason: {e.reason}")

    step("Audit trail for this run:")
    for record in guard.get_run_audit(run_id):
        result(f"  {record.tool_name:<20} -> {record.decision:<18} | {record.reason}")

    # ==================================================================
    # DEMO 2: Loop Detection
    # ==================================================================
    banner("DEMO 2: Loop Detection")
    run_id = "demo-run-002"

    step("Agent calls search_database 3 times in a row with the same arguments")
    for i in range(1, 4):
        try:
            search_database("invoice records", run_id=run_id)
            result(f"Call {i}: ALLOWED")
        except ToolDeniedException as e:
            result(f"Call {i}: DENIED -- {e.reason}")

    step("Audit trail for this run:")
    for record in guard.get_run_audit(run_id):
        result(f"  {record.tool_name:<20} -> {record.decision:<18} | {record.reason}")

    # ==================================================================
    # DEMO 3: Budget Exhaustion
    # ==================================================================
    banner("DEMO 3: Budget Exhaustion")
    run_id = "demo-run-003"
    result("Policy configures max_tool_calls: 3 for this run")
    result("Budget is a single counter shared across ALL tools in a run --")
    result("calling 4 DIFFERENT tools still hits the same shared limit.")

    step("Agent calls 4 different tools, one call each")
    calls = [
        ("fetch_data", fetch_data, "api-a"),
        ("fetch_orders", fetch_orders, "api-b"),
        ("fetch_inventory", fetch_inventory, "api-c"),
        ("fetch_shipments", fetch_shipments, "api-d"),
    ]
    for i, (tool_label, tool_fn, source) in enumerate(calls, start=1):
        try:
            tool_fn(source, run_id=run_id)
            result(f"Call {i} ({tool_label}): ALLOWED")
        except ToolDeniedException as e:
            result(f"Call {i} ({tool_label}): DENIED -- {e.reason}")

    step("Audit trail for this run:")
    for record in guard.get_run_audit(run_id):
        result(f"  {record.tool_name:<20} -> {record.decision:<18} | {record.reason}")

    # ==================================================================
    # Summary
    # ==================================================================
    banner("SUMMARY")
    all_runs = ["demo-run-001", "demo-run-002", "demo-run-003"]
    total_events = sum(len(guard.get_run_audit(r)) for r in all_runs)
    print(f"\nTotal audit records written across all demo runs: {total_events}")
    print(f"Audit database: {DB_PATH}")
    print("\nEvery decision above was made by deterministic policy evaluation.")
    print("No LLM, no external API call, no network request was made at any point.")
    print()

    guard._db.close_thread_connection()


if __name__ == "__main__":
    main()