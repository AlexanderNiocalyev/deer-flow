# DeerFlow upstream sync and Cloud Run deployment workflow

This fork treats `AlexanderNiocalyev/deer-flow` as the production source of truth.
The official `bytedance/deer-flow` repository is only a read-only upstream vendor source.

```text
bytedance/deer-flow          official upstream, fetch only
        ↓ manual/PR sync
AlexanderNiocalyev/deer-flow production fork, origin/main
        ↓ tested deploy
Cloud Run production DeerFlow
```

## Git remotes

Local clones should keep this remote layout:

```bash
git remote set-url origin https://github.com/AlexanderNiocalyev/deer-flow.git
git remote add upstream https://github.com/bytedance/deer-flow.git 2>/dev/null || \
  git remote set-url upstream https://github.com/bytedance/deer-flow.git
git remote set-url --push upstream DISABLED
```

## Weekly upstream check

`.github/workflows/upstream-sync.yml` runs every Monday at 02:00 UTC and can also be run manually.

It does this:

1. Fetches `origin/main` from this fork.
2. Fetches `upstream/main` from `bytedance/deer-flow`.
3. If there are no new upstream commits, exits without changing anything.
4. If upstream has new commits, creates or updates branch `sync/upstream-main`.
5. Attempts a normal git merge from upstream into the fork branch.
6. Opens or updates a PR into this fork's `main`.
7. If the merge conflicts, opens/updates a GitHub issue instead of forcing anything.

It intentionally does **not** auto-merge upstream into `main`.

## Required PR verification before merge

Every upstream sync PR should pass:

- backend unit tests
- frontend unit tests
- lint/check workflows
- Docker/Cloud Build path, if runtime/build files changed
- Cloud Run staging deployment, if backend/frontend/runtime/deploy behavior changed
- Gateway `/health` smoke test on staging
- Orpheus workspace / signed embed / sandbox flow smoke test

Only after those checks are green should the PR be merged to `main`.

## Cloud Run deployment

`.github/workflows/cloud-run-deploy.yml` deploys from this fork to Cloud Run.

Modes:

- manual `workflow_dispatch`: staging or production
- optional automatic production deploy on `main` pushes, only after setting repo/environment variable `CLOUD_RUN_DEPLOY_ON_MAIN=true`

Keep automatic production deploy disabled until branch protection and required checks are configured.

### Required GitHub settings

Use GitHub Environments named `staging` and `production` when possible.

Required secrets:

```text
GCP_WORKLOAD_IDENTITY_PROVIDER
GCP_SERVICE_ACCOUNT
```

Required variable or secret:

```text
GCP_PROJECT_ID
```

Common variables:

```text
CLOUD_RUN_REGION=us-central1
ARTIFACT_REGISTRY_REPOSITORY=deerflow
DEERFLOW_BACKEND_SERVICE=deerflow-gateway
DEERFLOW_FRONTEND_SERVICE=deerflow-frontend
DEERFLOW_FRONTEND_PUBLIC_URL=https://...
ORPHEUS_AGENT_WORKSPACE_CALLBACK_URL=https://.../internal/agent-workspace/deerflow-callbacks
OPENAI_API_KEY_SECRET_NAME=deerflow-openai-api-key
DEERFLOW_EMBED_TOKEN_SECRET_NAME=deerflow-embed-token-secret
ORPHEUS_AGENT_WORKSPACE_CALLBACK_TOKEN_SECRET_NAME=orpheus-agent-workspace-callback-token
```

The Cloud Run workflow calls:

```bash
ops/deerflow/cloud-run/deploy-cloud-run.sh all
```

Then it verifies:

```bash
curl "$DEERFLOW_GATEWAY_URL/health"
```

## Manual upstream sync command

If manual sync is needed:

```bash
git checkout main
git pull --ff-only origin main
git fetch upstream main

git checkout -B sync/upstream-$(date +%Y%m%d) main
git merge --no-ff upstream/main
# resolve conflicts, run tests, deploy staging smoke test

git push -u origin HEAD
gh pr create --base main --title "chore: sync upstream DeerFlow $(date +%Y-%m-%d)"
```

## Production rule

Never deploy directly from `bytedance/deer-flow`.
Cloud Run production deploys only from this fork's `main` or an explicit release tag from this fork.
