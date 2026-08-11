# Release evidence

This file distinguishes implemented behavior from production evidence. Version 1.0 is not complete until every live gate below has current, destination-confirmed evidence.

## Verified locally

| Requirement | Evidence | Status |
| --- | --- | --- |
| Windows runtime dependencies | `speak-out doctor --json` confirmed Python 3.12, FFmpeg, FFprobe, unified config, and installed Chrome on 2026-08-10 | verified |
| Offline test suite | 97 tests passed on 2026-08-10, including concurrent publication attempts, exception-safe claim resolution, ambiguous VPS outcomes, single-read article snapshots, manifest-bound private video snapshots, retryable Windows cleanup with pending markers, legacy validation, Chrome reconnect/interrupt/owned-process cleanup, constrained publication states, and exact anchored social metadata readback; rerun after every release-gate change | verified |
| Package build | Source distribution and wheel built with `uv build` | verified |
| Project-local Agent Skill | Official Skill validator passed for `.agents/skills/speak-out` | verified |
| Landscape video | A fully local Product rendered and re-inspected a 1920×1080 H.264 MP4 with audio, duration 3.5 seconds | verified |
| Artifact integrity | Source, script, material, and render revisions reported valid SHA-256 manifests in the local smoke Product | verified |
| Independent run/retry state | Unit and CLI tests cover run IDs, independent branch continuation, exact retry, reconciliation, and legacy SQLite migration | verified offline |
| Preflight safety | Real candidate-Product dry-run reported missing article/cover/publication/script approvals and separate Edge TTS/Pexels transfer gates; execute mode unit tests prove blocked child processes are not started | verified offline |
| Secret and Product isolation | `git ls-files .local workspace` returned no tracked files; `git check-ignore` matched both paths | verified |

The ignored local smoke Product is `workspace/2026-08-10-local-render-smoke`. It is evidence on this machine, not repository content and not a portable release fixture.

## Production evidence still required

| Destination or gate | Required proof | Current status |
| --- | --- | --- |
| Personal website | New-project `speak-out` execution receives explicit VPS business success and the article is visible | pending user-approved live run |
| WeChat Official Account | The same VPS execution reports `wechatPushed: true` and the expected draft is visible | pending user-approved live run |
| Edge TTS default path | Exact approved narration is sent only after `--allow-edge-tts-data-transfer`, then produces valid audio and SRT | pending data-transfer authorization |
| Xiaohongshu | Visible dedicated Chrome Profile submits the approved shared MP4 and the creator page confirms success | pending login and publication approval |
| Douyin | Visible dedicated Chrome Profile submits the approved shared MP4 and the creator page confirms success | pending login and publication approval |
| Bilibili | Visible dedicated Chrome Profile submits the approved shared MP4 and the creator page confirms success | pending login and publication approval |
| Live reconciliation | At least one safe `waiting_for_user` retry or a destination-checked reconciliation is demonstrated without replaying another destination | pending live opportunity |
| Production log audit | Logs, screenshots, Git state, and terminal output contain no secret, cookie, token, or private credential | pending after live run |

## Rules for completing the gate

1. Use a real Product with hash-valid, explicitly approved revisions.
2. Run every mutating command as a dry-run first and show the exact plan.
3. Obtain separate confirmation for article publication, narration/material data transfer, the rendered video, platform copy, and each immediate social submission.
4. Let the user complete login, QR scanning, captcha, and account choices only in visible local Chrome.
5. Record destination-confirmed results; treat any post-action disconnect as `unknown`.
6. Do not mark version 1.0 complete merely because offline tests remain green.
