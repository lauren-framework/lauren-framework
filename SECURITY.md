# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅ Yes     |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security bugs.**

Report vulnerabilities by emailing **security@lauren-py.dev** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a minimal proof-of-concept
- The affected version(s)
- Any suggested fix (optional)

You will receive an acknowledgement within **48 hours** and a resolution
timeline within **7 days**.  We follow responsible disclosure: we will
coordinate a fix and publish a CVE before public disclosure.

## Scope

In scope:

- Remote code execution
- Authentication / authorisation bypasses
- Privilege escalation in the DI container or module system
- Denial of service via crafted HTTP, WebSocket, or SSE payloads
- Information disclosure from request state, headers, or cookies

Out of scope:

- Vulnerabilities in applications **built with** Lauren (report to the
  application owner)
- Issues already fixed in the latest release
- Security concerns in optional companion packages (`lauren-middlewares`,
  `lauren-logging`, `lauren-guards`) — report those to their respective
  repositories

## Security-relevant design invariants

- **Identity must come from `request.state`, never from the LLM or
  user-supplied parameters.**  Guards populate `request.state` before any
  handler runs; handlers and tools read identity exclusively from there.
- **The dispatch path does not call `inspect`, `get_type_hints`, or any
  reflective API.**  All reflection happens at startup during
  `LaurenFactory.create()`; the hot path is pure traversal.
- **No global mutable state.**  Every singleton lives inside a `DIContainer`
  scoped to a `LaurenApp`.  Multiple app instances coexist safely in one
  process.
