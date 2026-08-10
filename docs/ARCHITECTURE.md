# Architecture

## Shape

The product is a local Python CLI with deep Modules behind small Interfaces. A project-local Skill is an Agent-facing guide, not the workflow engine.

```text
Agent / human
    |
    v
CLI Interface
    |
    v
Pipeline Module
    |-- ProductWorkspace Module
    |-- ApprovalLedger Module
    |-- ArticlePublisher Interface -> FixedIpVpsPublisher Adapter
    |-- VideoRenderer Interface ----> StockExplainerRenderer Adapter
    |-- BrowserDriver Interface ----> LocalChromeCdpDriver Adapter
    `-- PlatformPublisher Interface -> Xiaohongshu / Douyin / Bilibili Adapters
```

## Domain model

The Product is the durable aggregate. Its user-readable `product.toml` identifies the Product. Revision directories contain `.artifact.json` SHA-256 manifests; SQLite approvals retain the content digest of the exact artifact or publication bundle. SQLite also records independent publication results, run IDs, stage attempts, exact replay commands, append-only reconciliation evidence, and redacted results.

Every side-effecting stage uses an idempotency key derived from Product ID, artifact revision, destination, and intended action. Immediately before an external publication, SQLite takes a `BEGIN IMMEDIATE` transaction and atomically claims that destination/key as `running`; concurrent Agents therefore cannot both cross the same publication seam. A `PublicationAttempt` resolves every acquired claim: exceptions before the external seam become retryable `failed`, while exceptions after the seam begins become `unknown`. A successful publication is terminal unless the user explicitly creates a new revision or duplicate-publication override.

## Module Interfaces

### ProductWorkspace

Create and load a Product, add immutable artifact revisions, and resolve the selected revision. Callers do not construct paths.

### Pipeline

Advance a Product through validated stages, enforce approvals, run independent branches, and return a structured run result. It owns ordering rules and retry selection.

### ArticlePublisher

Build a dry-run preview and publish one approved article bundle. The first Adapter preserves the current VPS request contract while moving local validation and secret lookup into Python.

### VideoRenderer

Accept an approved video specification and produce inspected output artifacts. The first Adapter internalizes the useful MoneyPrinterTurbo pipeline: material acquisition, narration, subtitles, preprocessing, composition, and FFmpeg encoding.

### BrowserDriver

Open a visible, dedicated Chrome Profile and provide navigation, DOM interaction, upload, download, and user-intervention primitives. The default Adapter uses local Chrome 116+ and CDP, can reconnect to an existing platform Profile, and can explicitly close only that dedicated session.

### PlatformPublisher

Validate platform metadata, confirm login, upload the approved MP4, submit immediate publication, and return a result. Required metadata must be read back from the anchored platform control before submission; Bilibili tag chips and the selected category are explicit examples. Each platform is an independent Adapter at this real seam.

## Upstream absorption

- MoneyPrinterTurbo: selectively reuse or adapt MIT-licensed media-processing logic. Do not vendor the whole application or depend on its runtime installation.
- AiToEarn: selectively reuse or adapt MIT-licensed provider schemas, state categories, and publishing logic. Do not require its cloud service, Relay, databases, or application server.
- WeChat Official Account: do not absorb AiToEarn's current implementation because it directly calls WeChat from the local/server process, uses permanent cover material, and submits free-publish after draft creation. Preserve the proven fixed-IP VPS behavior instead.

Direct or substantial upstream reuse must retain copyright and MIT notices in source headers and `THIRD_PARTY_NOTICES.md`.

## Failure model

Stage plans and attempts use `planned`, `running`, `waiting_for_user`, `succeeded`, `failed`, `partial`, `unknown`, `skipped`, and `blocked`. A timeout, connection loss, or unstructured server error after an external request is `unknown`, not `failed`, until reconciliation proves whether the destination accepted it. `partial` means a compound destination proved only part of its contract—for example, the website succeeded but WeChat draft creation failed.

Website/WeChat, rendering, and each social platform are separate publication domains. Success in one domain never implies success in another and is never rolled back by another domain's failure.

`acp run` is dry-run by default. Execution persists each exact child command and continues other independent stages after a failure. `acp retry` is deliberately narrower: it replays only the latest exact command for a named `failed` or `waiting_for_user` stage. `unknown`, `partial`, and successful results cannot be replayed until externally reconciled or replaced by a newly approved revision.

`acp reconcile` never queries or mutates a destination. It records the user's external verification as a new Run. `unknown` or interrupted work may be reconciled as absent, which enables one exact retry; `unknown`, interrupted, or partial work may be reconciled as succeeded. Historical attempts remain unchanged.
