# Deployment examples

These examples show how to deploy the same small Lauren ASGI application to
common hosting environments. They are intentionally minimal: the application
logic stays in Lauren, while each deployment target supplies its own runtime
entry point and infrastructure configuration.

## Examples

| Target | Approach | Example |
|---|---|---|
| Any container platform | Uvicorn in Docker | [docker/](docker/) |
| Google Cloud Run | Deploy the Docker image | [google-cloud-run/](google-cloud-run/) |
| Azure Container Apps | Deploy the Docker image | [azure-container-apps/](azure-container-apps/) |
| AWS Lambda | ASGI-to-Lambda adapter (Mangum) | [aws-lambda/](aws-lambda/) |
| Cloudflare Workers | Python Workers ASGI adapter | [cloudflare-workers/](cloudflare-workers/) |

The Docker example is the baseline for container platforms. Cloud Run and
Container Apps do not require framework-specific code: they run the same
container image.

## Compatibility notes

Lauren is an ASGI application, but an ASGI interface does not imply that every
host provides identical runtime behavior. Before adopting a serverless target,
verify its support for application lifespan, streaming responses, WebSockets,
background work, synchronous handlers, request cancellation, and shutdown.

In particular:

- The AWS example uses **Mangum** to adapt ASGI to the traditional Lambda
  event-handler model. It is not the newer AWS Lambda Web Functions runtime.
- The Cloudflare example uses the Python Workers ASGI adapter. Python Workers
  execute on Pyodide/WebAssembly, not conventional CPython. Check compatibility
  of your dependencies and workload before deploying production traffic.
- The container examples use Uvicorn and the standard ASGI lifecycle.

## Run the baseline locally

From the repository root:

```bash
cd examples/deployment/docker
docker build -t lauren-hello .
docker run --rm -p 8080:8080 lauren-hello
curl http://localhost:8080/
```

Expected response:

```json
{"message":"Hello from Lauren"}
```

Each subdirectory contains its own deployment instructions and prerequisites.
