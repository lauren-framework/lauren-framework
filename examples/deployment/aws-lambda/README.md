# AWS Lambda with API Gateway

This example adapts Lauren's ASGI application to the traditional AWS Lambda
Python event-handler interface with [Mangum](https://github.com/Kludex/mangum).
It is intended for an API Gateway HTTP API or REST API integration.

This is different from AWS Lambda Web Functions, which provide a managed
ASGI/WSGI web runtime. The Mangum approach works with the established Lambda
Python runtime and API Gateway event model.

## Prerequisites

- An AWS account and configured AWS CLI credentials.
- Python version supported by the Lambda runtime you select.
- An API Gateway HTTP API (or REST API) configured to invoke the function.

## Package the function

From this directory, create a clean package directory and install dependencies
into it:

```bash
rm -rf package function.zip
mkdir package
python -m pip install -r requirements.txt --target package
cp main.py package/
(cd package && zip -qr ../function.zip .)
```

Build the package using the same Python major/minor version and compatible
architecture as the Lambda runtime. For larger dependency sets, prefer a
container image or Lambda layer and account for Lambda's deployment package
size limits.

## Create the Lambda function

Create an execution role with the basic Lambda execution policy, then create
the function (replace the role ARN):

```bash
aws lambda create-function \
  --function-name lauren-hello \
  --runtime python3.13 \
  --handler main.handler \
  --role "$LAMBDA_EXECUTION_ROLE_ARN" \
  --zip-file fileb://function.zip \
  --timeout 30 \
  --memory-size 512
```

If the function already exists, update its code instead:

```bash
aws lambda update-function-code \
  --function-name lauren-hello \
  --zip-file fileb://function.zip
```

Create an API Gateway HTTP API with a Lambda proxy integration targeting this
function, and configure a catch-all route (for example, `ANY /{proxy+}` plus
`$default` if you also need the root path). Grant API Gateway permission to
invoke the function. The exact API Gateway setup is intentionally left to your
account's infrastructure-as-code workflow.

## Verify

Call the API Gateway endpoint:

```bash
curl "https://YOUR_API_ID.execute-api.YOUR_REGION.amazonaws.com/"
```

Expected response:

```json
{"message":"Hello from Lauren"}
```

## Operational notes

- Mangum translates API Gateway events into ASGI; it does not turn Lambda into
  a continuously running web server.
- Keep request-specific data out of module globals. Lambda may reuse execution
  environments between invocations.
- Use external shared storage for sessions, cache, and state that must survive
  across execution environments.
- Review API Gateway payload format, binary response handling, maximum request
  duration, and streaming support for your integration. Do not assume that
  every ASGI feature maps directly to API Gateway/Lambda behavior.
- Configure Lambda timeout and memory based on measured application behavior.

For an alternative, see
[AWS Lambda Web Functions for Python](https://docs.aws.amazon.com/lambda/latest/dg/web-functions-python.html),
which documents native ASGI and WSGI application support in its dedicated
runtime.
