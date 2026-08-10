# Domain context

Use these terms consistently in code, tests, CLI output, and documentation.

- **Product**: one article and every versioned artifact derived from it.
- **Artifact**: a file such as article MDX, WeChat HTML, cover, narration, subtitle, material, or rendered video.
- **Revision**: an immutable version of an approved artifact.
- **Run**: one attempted workflow execution identified by a run ID.
- **Stage**: an independently retryable step in a run.
- **Approval**: explicit user authorization recorded before an irreversible external action.
- **Publication**: an attempt against exactly one destination.
- **Adapter**: an implementation at a confirmed seam, such as website/WeChat, video rendering, browser control, or a social platform.

Confirmed test seams:

1. CLI — commands, exit codes, and JSON/human output.
2. Product workspace — product creation, revision selection, and artifact layout.
3. Pipeline — approval enforcement, stage ordering, independent results, and retries.
4. Website/WeChat publisher — dry-run request construction and confirmed publication result.
5. Video renderer — validated inputs to an inspected MP4 result.
6. Platform publisher — one platform attempt and its independently retryable result.
7. Browser driver — visible local Chrome sessions using a dedicated profile.

Tests cross these seams only. Tests do not assert private helper calls.
