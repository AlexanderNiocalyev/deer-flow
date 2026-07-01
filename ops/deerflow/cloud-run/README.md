# DeerFlow on Cloud Run

This deploys DeerFlow as the runtime owner for Orpheus native Agent Workspace:

```text
Orpheus Console -> DeerFlow native Workspace -> DeerFlow Gateway -> VercelSandboxProvider -> Vercel Sandbox
```

Use this only after the Vercel sandbox provider and DB-backed record store are
present. The production config sets:

```yaml
database.backend: postgres
run_events.backend: db
sandbox.use: deerflow.community.vercel_sandbox:VercelSandboxProvider
sandbox.vercel_record_store: database
```

## Required Secret Manager Values

Create these secrets before deploying:

```bash
gcloud secrets create deerflow-database-url --data-file=-
gcloud secrets create deerflow-vercel-token --data-file=-
gcloud secrets create deerflow-vercel-project-id --data-file=-
gcloud secrets create deerflow-better-auth-secret --data-file=-
gcloud secrets create deerflow-auth-jwt-secret --data-file=-
gcloud secrets create deerflow-internal-auth-token --data-file=-
```

Usually also set:

```bash
OPENAI_API_KEY_SECRET_NAME=deerflow-openai-api-key
VERCEL_TEAM_ID_SECRET_NAME=deerflow-vercel-team-id
DEERFLOW_EMBED_TOKEN_SECRET_NAME=deerflow-embed-token-secret
ORPHEUS_AGENT_WORKSPACE_CALLBACK_TOKEN_SECRET_NAME=orpheus-agent-workspace-callback-token
```

Do not put literal secret values in `config.yaml` or in the deploy script.
The Cloud Run template is tracked as `config.prod.yaml` so it is not confused
with a local, untracked developer `config.yaml`.

## Deploy

```bash
export PROJECT_ID=<google-cloud-project>
export CLOUD_RUN_REGION=us-central1
export ORPHEUS_AGENT_WORKSPACE_CALLBACK_URL=https://<orpheus>/internal/agent-workspace/deerflow-callbacks
export DEERFLOW_FRONTEND_PUBLIC_URL=https://agent.example.com

ops/deerflow/cloud-run/deploy-cloud-run.sh all
```

By default both Cloud Run services are deployed with unauthenticated invocation
allowed so the frontend can proxy Gateway requests through its Next.js rewrites.
Keep DeerFlow auth and the Orpheus signed embed token enabled at the application
layer. If you put the services behind a Cloud Run load balancer, Cloudflare
Access, or service-to-service IAM, set:

```bash
export DEERFLOW_BACKEND_ALLOW_UNAUTHENTICATED=0
export DEERFLOW_FRONTEND_ALLOW_UNAUTHENTICATED=0
```

The first production revision intentionally uses one Gateway instance:

```text
DEERFLOW_BACKEND_MAX_INSTANCES=1
DEERFLOW_BACKEND_CONCURRENCY=10
DEERFLOW_BACKEND_TIMEOUT=3600
```

Keep that until RunManager, StreamBridge, cancel/reconnect, and request
deduplication are backed by shared infrastructure instead of process memory.

## Smoke Test

After deploy, check Gateway health:

```bash
curl "$(gcloud run services describe deerflow-gateway \
  --project "$PROJECT_ID" \
  --region "$CLOUD_RUN_REGION" \
  --format='value(status.url)')/health"
```

Then open the frontend URL and run a workspace task that writes a file. The
first sandbox acquisition should create a Vercel Sandbox and persist its binding
in the DeerFlow `runtime_bindings` table, not in a local JSON file.
