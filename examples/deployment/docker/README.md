# Docker (generic ASGI deployment)

This example runs Lauren under Uvicorn in a container. The resulting OCI image
can be deployed to any container platform that accepts HTTP traffic.

## Build and run

From this directory:

```bash
docker build -t lauren-hello .
docker run --rm -p 8080:8080 lauren-hello
curl http://localhost:8080/
```

The container listens on `0.0.0.0` and uses the `PORT` environment variable
when provided by the hosting platform (default: `8080`).

## Production considerations

- Put secrets in the platform's secret manager; do not bake them into the image.
- Use a shared external store for sessions, cache, and other state that must
  survive across replicas.
- Configure graceful termination and allow enough time for in-flight requests
  and Lauren's application shutdown hooks to finish.
- Set CPU and memory limits based on measured workload behavior.
- Add `HealthModule.for_root()` and configure platform health probes if the
  service needs explicit startup, liveness, and readiness checks.

The image intentionally runs one Uvicorn process. Scale with container
replicas first; if you choose multiple workers per container, size the total
process and connection count against the platform's CPU and memory allocation.
