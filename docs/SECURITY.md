# Security model

## User-selected secret storage

The user explicitly chose one plaintext, editable `.local/secrets.toml` file instead of an operating-system credential store. The project must make this tradeoff visible and reduce accidental exposure without claiming encryption.

- Never create a real secret file inside a tracked path.
- Refuse to load secrets from a path currently tracked by Git.
- Warn if `.local/` is not ignored.
- Redact secret values and credential-shaped headers from logs and errors.
- Never copy browser cookies into `secrets.toml`, Product directories, or logs.

## Browser profiles

Each platform uses a dedicated Chrome Profile under `.local/browser-profiles/`. The program must state which site will open and what login state will be retained before launching Chrome. The user's default Chrome Profile is out of scope.

## External actions

Production article publication and video-platform submission require recorded approval. Timeouts after submission produce an `unknown` state and require reconciliation before retry.

Edge TTS is an online service: the narration text leaves the machine. Pexels search terms leave the machine when remote stock materials are selected. Local-material rendering with pre-existing audio is the data-minimizing alternative. The CLI must not silently replace one with the other.

Article publication sends the approved MDX, WeChat HTML, and cover to the configured VPS. Social publication exposes only the approved MP4 and platform copy to the selected creator page in visible Chrome.

## Reporting

Do not include real credentials, cookies, publication payloads containing private drafts, or private logs in public issues. Revoke affected credentials immediately if exposure is suspected.
