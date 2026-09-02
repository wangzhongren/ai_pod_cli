# Distributed Orders Example

From `aipod-node/` after `npm run build`:

```bash
export AIPOD_BROKER_TOKEN="local-development-token"

npm run cli -- broker --port 8787 \
  --project-root examples/distributed-orders

npm run cli -- worker \
  --broker http://127.0.0.1:8787 \
  --token "$AIPOD_BROKER_TOKEN" \
  --stream orders --group processors --route processOrder \
  --project-root examples/distributed-orders

npm run cli -- publish \
  --broker http://127.0.0.1:8787 \
  --token "$AIPOD_BROKER_TOKEN" \
  --stream orders --key order-1 \
  --payload '{"orderId":"order-1"}'
```
