# Approval and recovery policy

## Required approvals

Record distinct approval for:

1. article revision, after showing both its website text and exact WeChat layout preview;
2. WeChat cover revision;
3. website and WeChat production publication;
4. rendered video revision;
5. selected social platforms, platform copy, and immediate submission.

Approval applies only to the exact artifact revision shown to the user. A new revision invalidates the old approval.

The CLI verifies the revision's SHA-256 manifest before recording or consuming approval. Publication approval also binds the article+cover or video+platform-copy content digests. A directory name such as `v001` is not sufficient by itself.

## Idempotency

Treat Product, artifact revision, destination, and action as the idempotency identity. If a successful result already exists, stop unless the user explicitly authorizes a duplicate publication.

## Unknown results

A timeout or connection loss after an external request does not prove failure. Mark it unknown, query the destination or existing logs, and retry only after proving the original action did not succeed.

## Orchestrated retry

Start with `speak-out run` without `--execute`; it is a side-effect-free exact plan. A safe stage is `planned`; a stage with invalid or missing artifacts, missing exact approvals, missing Edge/Pexels transfer acknowledgement, or an unsafe prior state is `blocked` with explicit reasons. Never execute a blocked plan. After execution, inspect `speak-out product status --json` for each independent attempt.

Use `speak-out retry` only for the latest `failed` or `waiting_for_user` attempt of the named stage. Never use it directly for `unknown`, `partial`, or successful outcomes. After the user checks the real destination, `speak-out reconcile` may append `absent` for `unknown` or interrupted work, or `succeeded` for `unknown`, interrupted, or partial work. It cannot turn `partial` into absent. Create and approve a new artifact revision when a genuinely new publication is intended.

## Existing website and WeChat behavior

Use the configured fixed-IP VPS route. WeChat must stop at draft creation. Do not substitute AiToEarn's WeChat implementation and do not call `freepublish/submit`.

Duplicate website slugs must stop before the WeChat step. Use an alternate slug only after the user explicitly accepts a duplicate website article.
