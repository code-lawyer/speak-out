---
name: speak-out
description: Create and run a local content Product from article drafting through personal-site publication, WeChat draft creation, 16:9 explainer-video rendering, and confirmed Xiaohongshu, Douyin, and Bilibili publication. Use when an Agent is asked to turn notes or an article into coordinated written and video outputs, resume or retry a Product, inspect publishing status, or operate the speak-out CLI.
---

# Speak Out

Use the Agent for editorial and creative work. Use the `speak-out` CLI for deterministic validation, state, rendering, browser control, and external publication.

## Start safely

1. Locate the `speak-out` repository and run `uv run speak-out --help`.
2. Invoke only commands shown by the installed CLI. Do not invent a command from this document when the CLI does not expose it yet.
3. Create one Product with `uv run speak-out product create` before writing artifacts.
4. Keep every artifact inside the returned Product directory.
5. Never read secret values into the conversation. Ask the user to edit `.local/secrets.toml` themselves when configuration is missing.

Run `uv run speak-out doctor --project-root .` before the first Product. Treat an unignored `.local/` directory or Chrome older than 116 as a hard stop. If the maintainer is migrating the old article Skill, use `speak-out config migrate-legacy-article`; it copies the proven local VPS settings without printing them or changing the VPS.

Read [content-contract.md](references/content-contract.md) before creating article, cover, or video artifacts. Read [approval-policy.md](references/approval-policy.md) before any validation, publication, retry, or recovery action.

## Workflow

### Prepare the Product

- Turn the user's source material into a finalized Chinese opinion or educational article.
- Save raw notes and inputs with `speak-out artifact add-source`; keep them out of release article files.
- Do not place research notes, raw-source links, or editorial annotations in the release article unless the user explicitly requests them.
- Add the finalized MDX with `speak-out artifact add-article --mdx ...`; omit
  `--body-html` and `--wechat-html`. The CLI must generate the deterministic
  `wechat-editorial-v1` body and preview. Never hand-author release WeChat HTML.
- Open the returned article revision's `index.html` and show the exact WeChat
  layout before recording article approval. Article approval covers both the
  website MDX and the hash-bound WeChat layout in that revision.
- Generate exactly one separately approved landscape WeChat cover using the Agent's available image tool, or accept a user-provided cover.
- Derive one 16:9 explainer-video narration script and material terms from the approved article. Preserve facts and thesis; adapt for speech instead of reading formatting artifacts aloud.
- Save the video input as JSON with `schemaVersion`, complete `narration`, and one or more English `materialTerms`. The core does not generate missing prose.
- Save platform copy for all three targets as one JSON revision. Bilibili also requires a category label.

### Confirm the written branch

Show the final article, exact WeChat `index.html` preview, and cover separately.
Record article approval only after the user explicitly confirms both the text
and that exact WeChat layout. Record cover and production publication approval
separately. Dry-run first.

After approval, allow the website/WeChat publication branch and video-rendering branch to proceed independently. Do not roll back or repeat a successful article publication because video rendering fails.

Use `speak-out article approve-publication` to bind the exact article revision, cover revision, target slug, and WeChat action. Run `speak-out article preview` before `speak-out article publish`. Never reconstruct an approval key manually.

Prefer `speak-out run` after the preview and approvals. First invoke it without `--execute` and show the exact dry-run plan. Only add `--execute` after the user confirms that plan. The written and video branches may share one run because they are independent; social publication must wait for the newly rendered video to be shown and separately approved.

Treat every `blocked` line in the dry-run as a hard stop. Read all listed blockers to the user and resolve them explicitly; do not add `--execute` while any selected stage remains blocked. The preflight checks exact artifacts and approvals, Edge/Pexels transfer acknowledgements, and known prior publication states. Execute mode will not start a child process for a preflight-blocked stage.

### Confirm the video branch

Show the rendered MP4 path and preview. Require explicit approval of the video and platform copy before any social-platform submission.

Use the same approved 16:9 video for Xiaohongshu, Douyin, and Bilibili. Adapt only platform-required metadata.

Run `speak-out social preview` separately for every selected platform and show its immediate-publication target, title, body, tags, category, and exact video revision before `speak-out social approve-publication`.

Before Edge TTS, tell the user that narration text is sent to Microsoft's online speech service. Use `speak-out artifact add-video-material` plus `speak-out video render --material-revision ...` for local footage; omit the material revision only when the user has configured and accepts Pexels search/download.

For long Pexels-backed videos, prefer `--material-count 24` (maximum 36) and let
the acquisition adapter distribute clips across every approved material term.
Never replace a requested Pexels run with cached local footage without explicit
user approval.

Pexels downloads use the project-local immutable cache under
`.local/media-cache/pexels/`. A cache hit reuses the exact Pexels asset bytes by
hard link when possible; it is not a local-material substitution and does not
remove the need for `--allow-pexels-data-transfer`, because search terms still
go to Pexels. Run `speak-out media-cache import-workspace --project-root .` once to
seed the cache from existing Products, and `speak-out media-cache status` to inspect
logical cache size without reading secret values.

Pass `--allow-edge-tts-data-transfer` only after the user explicitly accepts that exact narration transfer. Pass `--allow-pexels-data-transfer` only after the user accepts that exact material-term transfer. For private narration, provide both local `--narration-audio` and `--subtitles`; this bypasses Edge TTS. Run `speak-out video inspect` on the finished revision, show the exact MP4, then record video approval.

### Publish and recover

- Open visible local Chrome windows when login, captcha, QR scanning, or platform confirmation is needed.
- Tell the user which platform will open and that its dedicated local Chrome Profile retains login state.
- Treat every platform as an independent publication. Continue other selected platforms after one fails unless the failure invalidates the shared video.
- For Bilibili, require the selected category and normalized tag-chip set to exactly equal the approved metadata when read back from their anchored controls before allowing Publish; a click, synthetic Enter event, subset, or extra chip is not confirmation.
- Use `speak-out retry --product ... --stage ...` without `--execute` to show the exact stored replay command. Retry only the named `failed` or `waiting_for_user` stage, and add `--execute` only after confirmation.
- Treat timeouts after submission as unknown. Query status before retrying.
- Never replay website or WeChat publication during a social-platform retry.
- Record `video` approval for the exact render, then use `speak-out social approve-publication` separately for each platform. `speak-out social publish` automatically opens or reconnects to that platform's dedicated Chrome Profile.
- If the CLI returns `waiting_for_user`, do not click Publish. Leave the visible window open, ask the user to complete login or required options only, then use `speak-out retry` for only that platform stage. The Adapter never reports `waiting_for_user` after clicking Publish.
- If the CLI returns `unknown`, stop. Do not click publish again until the platform page or account proves the first submission was absent.
- When browser work is finished, use `speak-out social close --project-root ... --platform ...`; it closes only that platform's dedicated Profile session.
- After the user personally verifies an `unknown` or interrupted action, use dry-run `speak-out reconcile` with a non-secret evidence note. Apply it only with `--execute --confirmed-by-user`. Never reconcile `partial` as absent.

Every new revision is hash-sealed. If integrity validation fails, create a new revision; never edit or reseal an already sealed revision. For a legacy revision with no manifest, use `speak-out artifact seal-legacy` only after the user confirms the current bytes are the intended baseline; sealing does not grant approval or bypass current article/cover validation.

## Hard stops

Stop before an external action when approval is absent, the selected artifact revision changed after approval, credentials are missing, a destination's prior result is unknown, or the CLI reports validation failure.

Never bypass these stops with browser clicks, direct HTTP requests, ad hoc scripts, or an Agent-specific browser tool.

The low-level `speak-out article publish` and `speak-out social publish` commands are also dry-run by default. Their `--execute` flag is required even when invoked outside `speak-out run`.
