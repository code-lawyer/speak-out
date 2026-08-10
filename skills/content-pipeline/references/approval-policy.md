# Approval and recovery policy

## Required approvals

Record distinct approval for:

1. article revision;
2. WeChat cover revision;
3. website and WeChat production publication;
4. rendered video revision;
5. selected social platforms, platform copy, and immediate submission.

Approval applies only to the exact artifact revision shown to the user. A new revision invalidates the old approval.

## Idempotency

Treat Product, artifact revision, destination, and action as the idempotency identity. If a successful result already exists, stop unless the user explicitly authorizes a duplicate publication.

## Unknown results

A timeout or connection loss after an external request does not prove failure. Mark it unknown, query the destination or existing logs, and retry only after proving the original action did not succeed.

## Existing website and WeChat behavior

Use the configured fixed-IP VPS route. WeChat must stop at draft creation. Do not substitute AiToEarn's WeChat implementation and do not call `freepublish/submit`.

Duplicate website slugs must stop before the WeChat step. Use an alternate slug only after the user explicitly accepts a duplicate website article.
