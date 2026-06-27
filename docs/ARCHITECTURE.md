# AgentGuard Architecture

This document describes the internal runtime architecture, decision precedence, approval lifecycle, storage model, and execution flow.

## Runtime Decision Flow


```mermaid
graph TD
    A[Agent calls @guard.tool-wrapped function] --> B[Interceptor builds ExecutionContext]

    B --> C[Policy Engine]
    C -->|DENY| Z[Blocked + Audit Logged]

    C -->|ALLOW / REQUIRE_APPROVAL| D[Budget Check]
    D -->|Exceeded| Z

    D -->|Within Budget| E[Loop Detection]
    E -->|Loop Detected| Z

    E -->|Passed| F{Original Policy Decision}

    F -->|ALLOW| G[Tool Executes + Audit Logged]

    F -->|REQUIRE_APPROVAL| H[Approval Manager]

    H -->|Pending| I[ApprovalRequiredException Raised]

    H -->|Already Approved| G

    H -->|Rejected / Expired| Z
```
