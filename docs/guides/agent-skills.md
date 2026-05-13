# Agent Skills

Lauren ships 60+ **SKILL.md** context packs that give any AI coding agent
full, pre-loaded expertise on every part of the framework — from scaffolding
a new project to wiring OAuth2, SQLAlchemy, Redis, Prometheus, and more.

## What are skills?

A skill is a directory containing a `SKILL.md` file with YAML frontmatter
(`name`, `description`) and Markdown body. Coding agents (Claude Code, Cursor,
GitHub Copilot, Continue, Codex CLI, and others) discover and load skills
automatically from `~/.claude/skills/` or your project's `.claude/skills/`
directory.

When an agent works on a Lauren project, the relevant skill's instructions
are injected into its context — so it knows the correct imports, patterns,
common errors, and where to look first.

## One-command install

The easiest way to install all Lauren skills into every agent you have:

```bash
npx skills add lauren-framework/lauren-framework
```

This uses the [Vercel Labs `skills` CLI](https://github.com/vercel-labs/skills),
which:

1. Fetches the `skills/` directory from the GitHub repo
2. Auto-detects which agents are installed (`~/.claude/`, `~/.cursor/`, etc.)
3. Copies each skill into the appropriate global skills directory

## Install for a specific agent

**Claude Code:**

```bash
claude skills add https://github.com/lauren-framework/lauren-framework
# or manually:
git clone https://github.com/lauren-framework/lauren-framework /tmp/lf
cp -r /tmp/lf/skills/* ~/.claude/skills/
```

**Cursor:**

```bash
cp -r /path/to/lauren-framework/skills/* ~/.cursor/skills/
```

**Project-scoped** (applies only to the current project, any agent):

```bash
npx skills add lauren-framework/lauren-framework --local
# installs to ./.agent/skills/ or ./.claude/skills/ depending on agent
```

## Skills index

### Core framework

| Skill | What it covers |
|---|---|
| [`building-lauren-apps`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/building-lauren-apps) | `LaurenFactory.create()`, `@module`, project layout, bootstrap |
| [`building-lauren-controllers`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/building-lauren-controllers) | `@controller`, HTTP decorators, extractors, pipes, serialization |
| [`building-lauren-services`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/building-lauren-services) | `@injectable`, DI scopes, lifecycle hooks, custom providers |
| [`building-lauren-guards`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/building-lauren-guards) | Guards, interceptors, middleware, `@use_guards` |
| [`building-lauren-streaming`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/building-lauren-streaming) | SSE, WebSocket gateways, `StreamingResponse[T]` |
| [`building-lauren-background-tasks`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/building-lauren-background-tasks) | `BackgroundTasks`, `TaskHandle`, fire-and-forget |
| [`testing-lauren-apps`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/testing-lauren-apps) | `TestClient`, `WsTestClient`, async tests, mock providers |
| [`common-patterns`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/common-patterns) | CRUD, health check, background job, typed SSE stream |

### Authentication & authorization

| Skill | What it covers |
|---|---|
| [`oauth2-integration`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/oauth2-integration) | OAuth2 authorization-code flow, provider integration |
| [`jwt-tokens`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/jwt-tokens) | JWT creation, `JWTBearerGuard`, 401 handling |
| [`jwt-refresh-rotation`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/jwt-refresh-rotation) | Refresh token rotation, JTI blacklisting |
| [`rbac-engine`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/rbac-engine) | Role-based access control, permission guards |
| [`abac-evaluation`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/abac-evaluation) | Attribute-based access control policy engine |
| [`session-store`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/session-store) | In-memory & Redis session stores, cookie middleware |
| [`mfa-totp`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/mfa-totp) | TOTP-based MFA with `pyotp` |
| [`api-key-auth`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/api-key-auth) | SHA-256 hashed API keys, scoped permissions |

### Database & search

| Skill | What it covers |
|---|---|
| [`sqlalchemy-models`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/sqlalchemy-models) | SQLAlchemy ORM, `@post_construct` lifecycle |
| [`sqlalchemy-async`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/sqlalchemy-async) | Async engine, `AsyncSession`, request-scoped sessions |
| [`alembic-migrations`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/alembic-migrations) | Alembic `upgrade` / `downgrade` patterns |
| [`multi-database-routing`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/multi-database-routing) | Primary/replica read-write routing |
| [`redis-caching`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/redis-caching) | TTL-based caching, prefix invalidation |
| [`postgres-fts`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/postgres-fts) | Full-text search, `tsvector` / `to_tsquery` |
| [`search-engine-integration`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/search-engine-integration) | Elasticsearch / Meilisearch abstract interface |

### Configuration & secrets

| Skill | What it covers |
|---|---|
| [`pydantic-settings-config`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/pydantic-settings-config) | `BaseSettings`, env var loading, injectable config |
| [`feature-flags`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/feature-flags) | Feature flags with rollout percentages |
| [`secrets-management`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/secrets-management) | Vault / AWS Secrets Manager abstract provider |
| [`environment-profiles`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/environment-profiles) | dev / staging / prod profile merging |
| [`config-hot-reload`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/config-hot-reload) | Dynamic config update API with async locking |

### API patterns & messaging

| Skill | What it covers |
|---|---|
| [`rest-crud-endpoints`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/rest-crud-endpoints) | Full CRUD with correct 201 / 204 / 404 status codes |
| [`graphql-integration`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/graphql-integration) | GraphQL endpoint mounting (Strawberry / Ariadne) |
| [`websocket-rooms`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/websocket-rooms) | WebSocket room management with `BroadcastGroup` |
| [`api-rate-limiting`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/api-rate-limiting) | Token-bucket rate limiting, `RateLimitGuard` |
| [`api-versioning`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/api-versioning) | URL prefix, Accept-Version header, content negotiation |
| [`message-queue`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/message-queue) | RabbitMQ / Kafka abstract producer & consumer |
| [`event-sourcing`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/event-sourcing) | Append-only event store, aggregate rebuild, projections |
| [`transactional-email`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/transactional-email) | SMTP / SendGrid / SES abstract email backend |
| [`push-notifications`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/push-notifications) | FCM / APNs / Web Push abstract backend |
| [`webhook-dispatcher`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/webhook-dispatcher) | HMAC-signed outbound webhooks with retry |

### Storage & media

| Skill | What it covers |
|---|---|
| [`object-storage`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/object-storage) | S3 / GCS / MinIO abstract object store |
| [`presigned-url-uploads`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/presigned-url-uploads) | Time-limited signed upload URLs |
| [`file-upload-validation`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/file-upload-validation) | MIME detection, size limits, virus scan hook |
| [`image-processing`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/image-processing) | Resize, crop, thumbnail, grayscale with Pillow |
| [`bulk-import-export`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/bulk-import-export) | CSV / Excel / JSON import-export pipeline |

### Background tasks & scheduling

| Skill | What it covers |
|---|---|
| [`background-task-scheduler`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/background-task-scheduler) | Lauren `BackgroundTasks` + Celery / ARQ patterns |
| [`cron-interval-jobs`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/cron-interval-jobs) | Interval job registration, `@post_construct` start |
| [`retry-dead-letter-queue`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/retry-dead-letter-queue) | Retry queue with exponential backoff, DLQ |

### Observability

| Skill | What it covers |
|---|---|
| [`structured-json-logging`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/structured-json-logging) | JSON log output, correlation ID middleware |
| [`prometheus-metrics`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/prometheus-metrics) | Counter, Histogram, `/metrics` endpoint |
| [`opentelemetry-tracing`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/opentelemetry-tracing) | Distributed tracing, in-memory span exporter |
| [`health-check-probes`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/health-check-probes) | `/health/live`, `/health/ready`, dependency checks |
| [`audit-log-trail`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/audit-log-trail) | Append-only audit log, query by user / resource |

### Security & compliance

| Skill | What it covers |
|---|---|
| [`field-level-encryption`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/field-level-encryption) | Fernet encryption, key rotation with `MultiFernet` |
| [`input-sanitization`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/input-sanitization) | SQL injection detection, XSS stripping, CSRF tokens |
| [`gdpr-data-requests`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/gdpr-data-requests) | Data subject export and deletion handler |
| [`security-headers-cors`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/security-headers-cors) | CSP, HSTS, CORS middleware |
| [`multi-tenant-isolation`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/multi-tenant-isolation) | Per-row tenant isolation, `ContextVar` middleware |

### Migration & architecture

| Skill | What it covers |
|---|---|
| [`migrating-from-fastapi`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/migrating-from-fastapi) | FastAPI → Lauren: routing, DI, middleware side-by-side |
| [`using-companion-packages`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/using-companion-packages) | CORS, auth guards, structured logging together |
| [`building-companion-packages`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/building-companion-packages) | Package layout, DI/module integration, `llms*.txt`, CI/CD, and publishing for companion packages |
| [`graceful-shutdown`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/graceful-shutdown) | Connection draining, `@pre_destruct` shutdown hooks |
| [`docker-compose-setup`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/docker-compose-setup) | Multi-stage Dockerfile, docker-compose stack |

## LLM context files

For agents that ingest raw context rather than SKILL.md files:

| File | Purpose |
|---|---|
| [`llms.txt`](https://lauren-py.dev/llms.txt) | 2 KB overview — start here |
| [`llms-full.txt`](https://github.com/lauren-framework/lauren-framework/raw/main/lauren/llms-full.txt) | 25 KB complete API reference |
| [`AGENTS.md`](https://github.com/lauren-framework/lauren-framework/blob/main/AGENTS.md) | By-task lookup, common errors, definition of done |
| [`CLAUDE.md`](https://github.com/lauren-framework/lauren-framework/blob/main/CLAUDE.md) | Architecture invariants, pattern selection |
