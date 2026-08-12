# Release evidence

This file distinguishes implemented behavior from production evidence. Version 1.0 is not complete until every live gate below has current, destination-confirmed evidence.

## Verified locally

| Requirement | Evidence | Status |
| --- | --- | --- |
| Windows runtime dependencies | `speak-out doctor --json` confirmed Python 3.12, FFmpeg, FFprobe, unified config, and installed Chrome on 2026-08-10 | verified |
| Offline test suite | 129 tests passed on 2026-08-12, including concurrent publication attempts, exception-safe claim resolution, ambiguous VPS outcomes, exact article/video snapshots, provenance-checked media-cache import, retryable Windows cleanup, legacy validation, Chrome cleanup, exact anchored metadata readback, explicit remote upload gating, manifest-bound retained snapshots, interrupted-upload retention, and stale failed-page retry protection | verified |
| Package build | Source distribution and wheel built with `uv build` | verified |
| Project-local Agent Skill | Official Skill validator passed for `.agents/skills/speak-out` | verified |
| Landscape video | Product `ai-is-coming-for-lawyers` produced two inspected 1920×1080 H.264 MP4 revisions with audio and 495.1-second duration | verified |
| Edge TTS and Pexels path | The approved script produced `v002` with Edge TTS and 24 remotely acquired Pexels clips after both exact transfer acknowledgements | verified live |
| Artifact integrity | Source, script, material, and render revisions reported valid SHA-256 manifests in the local smoke Product | verified |
| Independent run/retry state | Unit and CLI tests cover run IDs, independent branch continuation, exact retry, reconciliation, and legacy SQLite migration | verified offline |
| Preflight safety | Real candidate-Product dry-run reported missing article/cover/publication/script approvals and separate Edge TTS/Pexels transfer gates; execute mode unit tests prove blocked child processes are not started | verified offline |
| Secret and Product isolation | `git ls-files .local workspace` returned no tracked files; `git check-ignore` matched both paths | verified |

The ignored local smoke Product is `workspace/2026-08-10-local-render-smoke`. It is evidence on this machine, not repository content and not a portable release fixture.

## Production evidence still required

| Destination or gate | Required proof | Current status |
| --- | --- | --- |
| Clean public clone | Clone `code-lawyer/speak-out` into a new directory and complete setup, doctor, tests, Skill validation, and a new Product | pending final acceptance run |
| Xiaohongshu | Visible dedicated Chrome Profile submits the approved shared MP4 and the creator page confirms success | pending post-fix live revalidation; first attempt failed after the old workflow deleted the source at 0% |
| Douyin | Visible dedicated Chrome Profile submits the approved shared MP4 and the creator page confirms success | pending post-fix live revalidation; first attempt failed after the old workflow deleted the source at 1% |
| Bilibili | Visible dedicated Chrome Profile submits the approved shared MP4 and the creator page confirms success | pending post-fix live revalidation; first attempt never entered the platform upload state |
| Live reconciliation | At least one safe `waiting_for_user` retry or a destination-checked reconciliation is demonstrated without replaying another destination | pending live opportunity |
| Production log audit | Logs, screenshots, Git state, and terminal output contain no secret, cookie, token, or private credential | pending after live run |

## Destination-confirmed production evidence

| Destination or gate | Evidence | Status |
| --- | --- | --- |
| Personal website | Product `ai-is-coming-for-lawyers`, publication key `v001+v001+ai-is-coming-for-lawyers+wechat`; local ledger recorded `succeeded` and the user confirmed the article was visible | verified 2026-08-10 |
| WeChat Official Account | The same fixed-IP VPS attempt completed and the user confirmed the expected draft appeared in the Official Account draft box | verified 2026-08-10 |

## Rules for completing the gate

1. Use a real Product with hash-valid, explicitly approved revisions.
2. Run every mutating command as a dry-run first and show the exact plan.
3. Obtain separate confirmation for article publication, narration/material data transfer, the rendered video, platform copy, and each immediate social submission.
4. Let the user complete login, QR scanning, captcha, and account choices only in visible local Chrome.
5. Record destination-confirmed results; treat any post-action disconnect as `unknown`.
6. Do not mark version 1.0 complete merely because offline tests remain green.
