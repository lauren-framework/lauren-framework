# Google Cloud Run

Cloud Run runs the same container image as the [Docker example](../docker/).
No Cloud Run-specific adapter is needed: Lauren remains a normal ASGI
application served by Uvicorn.

## Prerequisites

- A Google Cloud project with billing enabled.
- The Google Cloud CLI installed and authenticated.
- The Cloud Run and Artifact Registry APIs enabled.

Set your project and region:

```bash
export PROJECT_ID="your-project-id"
export REGION="asia-southeast1"
gcloud config set project "$PROJECT_ID"
```

## Build and deploy from this directory

Build the container from the shared Docker example and submit it to Cloud
Build:

```bash
gcloud builds submit ../docker \
  --tag "$REGION-docker.pkg.dev/$PROJECT_ID/lauren-examples/hello:latest"
```

If the Artifact Registry repository does not exist yet, create it first:

```bash
gcloud artifacts repositories create lauren-examples \
  --repository-format=docker \
  --location="$REGION"
```

Deploy the image:

```bash
gcloud run deploy lauren-hello \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/lauren-examples/hello:latest" \
  --region "$REGION" \
  --port 8080 \
  --allow-unauthenticated
```

Remove `--allow-unauthenticated` if the service should require authenticated
invocation. Grant the Cloud Build service account permission to write to the
Artifact Registry repository if it does not already have it.

## Verify

Use the service URL printed by the deploy command:

```bash
curl "$(gcloud run services describe lauren-hello \
  --region "$REGION" --format='value(status.url)')/"
```

Cloud Run injects the `PORT` environment variable; the shared Docker image
uses it and defaults to port 8080 for local development.

## Operational notes

- Configure minimum and maximum instances, CPU, memory, and concurrency for
  your workload rather than relying blindly on defaults.
- Use Secret Manager-backed environment variables or mounted secrets for
  credentials.
- Keep request-specific state out of module globals. Use a shared external
  store for state that must persist across instances.
- Review Cloud Run's request timeout and termination behavior when using
  long-lived SSE or streaming responses.

See the official
[Cloud Run container deployment guide](https://cloud.google.com/run/docs/deploying).
