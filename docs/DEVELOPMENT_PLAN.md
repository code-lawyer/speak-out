# Development plan

## Phase 0 — repository and contracts

- Establish packaging, license, third-party notices, security policy, Product layout, domain vocabulary, and test seams.
- Implement `acp doctor`, `acp config`, `acp product create`, `acp product status`.
- Prove reproducible setup with Python 3.12 and uv on Windows.

Exit: a clean clone can create and inspect a Product without external accounts.

## Phase 1 — article compatibility

- Port local MDX, WeChat HTML, and PNG validation from the existing Skill.
- Implement unified secret loading and redaction.
- Implement the current VPS request contract and dry-run output.
- Reproduce duplicate-slug recovery and unknown-timeout handling.
- Compare generated request bodies against the legacy `push.mjs` through contract tests.

Exit: local dry-run parity is proven. A separately approved production test creates the website article and WeChat draft without changing the VPS.

## Phase 2 — video engine

- Audit the current MoneyPrinterTurbo main branch and record reused modules and licenses.
- Implement Pexels and local-material acquisition.
- Implement Edge TTS narration, SRT generation, BGM mixing, and 16:9 material normalization.
- Implement FFmpeg composition and FFprobe output inspection.
- Preserve every intermediate artifact in the Product directory.

Exit: an approved script produces a playable 1920x1080 H.264 MP4 with narration and subtitles.

## Phase 3 — browser foundation

- Discover local Chrome and validate a supported version.
- Launch a visible Chrome process with a dedicated non-default profile and an ephemeral CDP port.
- Implement safe shutdown, user-intervention waits, screenshots, redacted traces, uploads, and selector diagnostics.
- Add optional AgentBrowserDriver and PlaywrightDriver Interfaces without making them required.

Exit: the CLI opens, reconnects to, and closes one platform Profile without touching the user's default Chrome Profile.

## Phase 4 — platform publishers

- Bilibili first: login, validate partition/tags, upload, submit, and reconcile result.
- Xiaohongshu second: visible login, landscape-video upload, metadata, submit, and result capture.
- Douyin third: visible login, landscape-video upload, metadata, submit or wait for required user action, and result capture.
- Keep selectors, endpoints, validation rules, and platform errors local to each Adapter.

Exit: each Adapter passes offline contract tests and one separately confirmed live publication.

## Phase 5 — orchestration and Agent Skill

- Run article publication and video rendering as independent branches.
- Add video preview and distribution approval gates.
- Add per-stage retry, reconciliation, and idempotency safeguards.
- Initialize and validate a project-local `content-pipeline` Skill with concise instructions and progressive references.
- Add `acp run`, `acp retry`, and machine-readable JSON output.

Exit: the full v1 release gate in `PRODUCT.md` is proven with current-state evidence.

Implementation status: the offline orchestration slice is complete. `acp run` is dry-run by default, assigns a durable run ID, and preflights artifact integrity, exact approvals, Edge/Pexels transfer acknowledgements, and prior publication state before showing `planned` or explicit `blocked` stages. Execute mode does not start a child process for a preflight-blocked stage. External destinations are atomically claimed before publication, and ambiguous VPS transport/server outcomes remain `unknown` until reconciliation. Independent branches run without rollback and record centrally redacted stage attempts. `acp retry` replays only the latest `failed` or safely pre-submit `waiting_for_user` stage. `acp reconcile` appends explicit human evidence to resolve `unknown` or interrupted outcomes without rewriting history. Artifact SHA-256 manifests bind approvals to exact bytes, while legacy article bytes are still revalidated at every publication boundary. The visible Chrome 116+ Driver now supports version checks, reconnect, and explicit dedicated-Profile shutdown. Source revisions, video inspection, legacy state/artifact migration, explicit Edge/Pexels transfer gates, and a fully local narration option are implemented and tested. The phase exit remains open until the separately approved live release gate is completed.

## Verification discipline

- Build vertical slices: one failing behavior test, minimal implementation, repeat.
- Unit tests cross only the confirmed public seams in `CONTEXT.md`.
- Network tests use captured schemas and local fakes by default.
- Live tests are opt-in, require explicit confirmation, and never run in ordinary CI.
- A green offline suite is necessary but not sufficient for v1; every named live destination needs direct evidence.
