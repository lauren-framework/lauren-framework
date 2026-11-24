# Azure Container Apps

Azure Container Apps runs the same Uvicorn container as the
[Docker example](../docker/). Lauren does not need an Azure-specific ASGI
adapter.

## Prerequisites

- An Azure subscription.
- Azure CLI installed and authenticated (`az login`).
- Permission to create a resource group, Container Apps environment, and
  Container App.

Set the deployment values:

```bash
export RESOURCE_GROUP="rg-lauren-examples"
export LOCATION="southeastasia"
export APP_NAME="lauren-hello"
```

## Deploy from source

From this directory, build and deploy the Docker example:

```bash
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION"

az containerapp up \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --source ../docker \
  --ingress external \
  --target-port 8080
```

The command creates or uses the required Container Apps environment and builds
the source into a container image. If your organization requires a private
registry or pre-created environment, follow your platform's image and
environment provisioning policy instead.

## Verify

Retrieve the application URL and make a request:

```bash
APP_URL="$(az containerapp show \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn \
  --output tsv)"

curl "https://$APP_URL/"
```

## Operational notes

- Use managed identity to access Azure services where possible.
- Store credentials in Key Vault and expose them through Container Apps
  secrets or secret references.
- Configure ingress, minimum/maximum replicas, scaling rules, CPU, and memory
  for your workload.
- Use a shared external store for state that must be consistent across
  replicas.
- For long-lived streams, check ingress and client timeout behavior.

See the official
[Azure Container Apps documentation](https://learn.microsoft.com/azure/container-apps/).
