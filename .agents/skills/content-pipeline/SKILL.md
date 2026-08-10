---
name: content-pipeline
description: Create and run a local content Product from article drafting through personal-site publication, WeChat draft creation, 16:9 explainer-video rendering, and confirmed Xiaohongshu, Douyin, and Bilibili publication. Use when an Agent is asked to turn notes or an article into coordinated written and video outputs, resume or retry a Product, inspect publishing status, or operate the agent-content-pipeline CLI.
---

# Content Pipeline

Use the Agent for editorial and creative work. Use the `acp` CLI for deterministic validation, state, rendering, browser control, and external publication.

## Start safely

1. Locate the `agent-content-pipeline` repository and run `uv run acp --help`.
2. Invoke only commands shown by the installed CLI. Do not invent a command from this document when the CLI does not expose it yet.
3. Create one Product with `uv run acp product create` before writing artifacts.
4. Keep every artifact inside the returned Product directory.
5. Never read secret values into the conversation. Ask the user to edit `.local/secrets.toml` themselves when configuration is missing.

Run `uv run acp doctor --project-root .` before the first Product. If the maintainer is migrating the old article Skill, use `acp config migrate-legacy-article`; it copies the proven local VPS settings without printing them or changing the VPS.

Read [content-contract.md](references/content-contract.md) before creating article, cover, or video artifacts. Read [approval-policy.md](references/approval-policy.md) before any validation, publication, retry, or recovery action.

## Workflow

### Prepare the Product

- Turn the user's source material into a finalized Chinese opinion or educational article.
- Save raw notes and inputs with `acp artifact add-source`; keep them out of release article files.
- Do not place research notes, raw-source links, or editorial annotations in the release article unless the user explicitly requests them.
- Create WeChat HTML without inline illustrations.
- Generate exactly one separately approved landscape WeChat cover using the Agent's available image tool, or accept a user-provided cover.
- Derive one 16:9 explainer-video narration script and material terms from the approved article. Preserve facts and thesis; adapt for speech instead of reading formatting artifacts aloud.
- Save the video input as JSON with `schemaVersion`, complete `narration`, and one or more English `materialTerms`. The core does not generate missing prose.
- Save platform copy for all three targets as one JSON revision. Bilibili also requires a category label.

### Confirm the written branch

Show the final article and cover separately. Record approval only after the user explicitly confirms the article, cover, and production publication. Dry-run first.

After approval, allow the website/WeChat publication branch and video-rendering branch to proceed independently. Do not roll back or repeat a successful article publication because video rendering fails.

Use `acp article approve-publication` to bind the exact article revision, cover revision, target slug, and WeChat action. Run `acp article preview` before `acp article publish`. Never reconstruct an approval key manually.

Prefer `acp run` after the preview and approvals. First invoke it without `--execute` and show the exact dry-run plan. Only add `--execute` after the user confirms that plan. The written and video branches may share one run because they are independent; social publication must wait for the newly rendered video to be shown and separately approved.

### Confirm the video branch

Show the rendered MP4 path and preview. Require explicit approval of the video and platform copy before any social-platform submission.

Use the same approved 16:9 video for Xiaohongshu, Douyin, and Bilibili. Adapt only platform-required metadata.

Run `acp social preview` separately for every selected platform and show its immediate-publication target, title, body, tags, category, and exact video revision before `acp social approve-publication`.

Before Edge TTS, tell the user that narration text is sent to Microsoft's online speech service. Use `acp artifact add-video-material` plus `acp video render --material-revision ...` for local footage; omit the material revision only when the user has configured and accepts Pexels search/download.

Pass `--allow-edge-tts-data-transfer` only after the user explicitly accepts that exact narration transfer. Pass `--allow-pexels-data-transfer` only after the user accepts that exact material-term transfer. For private narration, provide both local `--narration-audio` and `--subtitles`; this bypasses Edge TTS. Run `acp video inspect` on the finished revision, show the exact MP4, then record video approval.

### Publish and recover

- Open visible local Chrome windows when login, captcha, QR scanning, or platform confirmation is needed.
- Tell the user which platform will open and that its dedicated local Chrome Profile retains login state.
- Treat every platform as an independent publication. Continue other selected platforms after one fails unless the failure invalidates the shared video.
- Use `acp retry --product ... --stage ...` without `--execute` to show the exact stored replay command. Retry only the named `failed` or `waiting_for_user` stage, and add `--execute` only after confirmation.
- Treat timeouts after submission as unknown. Query status before retrying.
- Never replay website or WeChat publication during a social-platform retry.
- Record `video` approval for the exact render, then use `acp social approve-publication` separately for each platform. `acp social publish` automatically opens or reconnects to that platform's dedicated Chrome Profile.
- If the CLI returns `waiting_for_user`, leave the visible window open, ask the user to complete login or required options, then use `acp retry` for only that platform stage.
- If the CLI returns `unknown`, stop. Do not click publish again until the platform page or account proves the first submission was absent.
- After the user personally verifies an `unknown` or interrupted action, use dry-run `acp reconcile` with a non-secret evidence note. Apply it only with `--execute --confirmed-by-user`. Never reconcile `partial` as absent.

Every new revision is hash-sealed. If integrity validation fails, create a new revision; never edit or reseal an already sealed revision. For a legacy revision with no manifest, use `acp artifact seal-legacy` only after the user confirms the current bytes are the intended baseline; sealing does not grant approval.

## Hard stops

Stop before an external action when approval is absent, the selected artifact revision changed after approval, credentials are missing, a destination's prior result is unknown, or the CLI reports validation failure.

Never bypass these stops with browser clicks, direct HTTP requests, ad hoc scripts, or an Agent-specific browser tool.

The low-level `acp article publish` and `acp social publish` commands are also dry-run by default. Their `--execute` flag is required even when invoked outside `acp run`.
