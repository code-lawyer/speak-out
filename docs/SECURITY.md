# Security model

## User-selected secret storage

The user explicitly chose one plaintext, editable `.local/secrets.toml` file instead of an operating-system credential store. The project must make this tradeoff visible and reduce accidental exposure without claiming encryption.

- Never create a real secret file inside a tracked path.
- Refuse to load secrets from a path currently tracked by Git.
- Warn if `.local/` is not ignored.
- Redact secret values and credential-shaped headers from logs and errors.
- Never copy browser cookies into `secrets.toml`, Product directories, or logs.

## Browser profiles

Each platform uses a dedicated Chrome Profile under `.local/browser-profiles/`. The program must state which site will open and what login state will be retained before launching Chrome. Chrome 116 or newer is required and checked by `speak-out doctor` and again over CDP. `speak-out social close` closes only the selected dedicated Profile session. The user's default Chrome Profile is out of scope.

Failed or uncertain browser publication may save a diagnostic screenshot under the ignored Product `logs/` directory. It can contain visible account-page information, so it must remain local and must not be attached to public issues without review.

## External actions

Production article publication and video-platform submission require recorded approval. Timeouts after submission produce an `unknown` state and require reconciliation before retry.

Edge TTS is an online service: the narration text leaves the machine. Pexels search terms leave the machine when remote stock materials are selected. Local-material rendering with pre-existing audio is the data-minimizing alternative. The CLI must not silently replace one with the other.

The CLI requires `--allow-edge-tts-data-transfer` before constructing the online narrator and `--allow-pexels-data-transfer` before remote material search. These flags are exact-command acknowledgements, not permanent consent. Supplying both local `--narration-audio` and `--subtitles` bypasses Edge TTS; supplying `--material-revision` bypasses Pexels.

Article publication sends one manifest-verified snapshot of the approved MDX, WeChat HTML, and cover to the configured VPS. Social publication reads copy from a verified snapshot and exposes a private, manifest-bound MP4 copy only to the selected creator page in visible Chrome; the temporary copy stays inside the Product and is removed after the attempt.

New WeChat layouts are deterministic and hash-bound to the article revision.
Immediately before publication, the Pipeline regenerates the canonical layout
from the verified MDX bytes and rejects body or preview drift. Exact article
approval therefore covers both written content and the displayed WeChat layout.

The Pexels cache contains public stock-video bytes and attribution metadata
only; it never contains credentials, authorization headers, narration, cookies,
or private drafts. Cache files are immutable. Product hard links may outlive the
cache directory entry, so removing a cache entry does not invalidate sealed
Product bytes.

Video rendering likewise uses the exact verified `script.json` bytes and a streamed Product-local snapshot of sealed local materials. Approved scripts and materials are never verified by path and then re-read from the mutable source path for TTS or FFmpeg.

Every artifact revision is sealed with a local SHA-256 manifest. Artifact and publication approvals retain the associated content digest. A missing or changed file blocks later approval or execution. Legacy revisions require an explicit seal operation and must then be approved normally. A valid legacy hash is not treated as content validity: the exact MDX, WeChat body/full HTML, and PNG bytes are revalidated before preview, publication approval, preflight, and execution.

## Reporting

Do not include real credentials, cookies, publication payloads containing private drafts, or private logs in public issues. Revoke affected credentials immediately if exposure is suspected.
