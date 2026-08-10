# Product specification

## Purpose

Build a local tool that an AI coding agent can use to turn an approved Chinese opinion or educational article into coordinated written and video publications without hiding irreversible actions from the user.

The first priority is preserving and extending the maintainer's working production path. General reproducibility for unrelated users is secondary.

## Users and runtime

- A user clones the repository and operates it through a local agent or directly through the CLI.
- The project has no hosted control plane, user database, subscription, or mandatory cloud relay.
- The first supported runtime is Windows. Interfaces and paths should avoid needless Windows coupling.
- The core does not call an LLM. The active agent creates prose, prompts, scripts, keywords, and platform copy.

## Required workflow

1. Create a Product directory.
2. Store source notes, article MDX, WeChat HTML, and a generated or user-provided WeChat cover.
3. Validate the article and cover.
4. Record explicit article, cover, and publication approvals.
5. Start two independent branches:
   - publish the personal-site article and create a WeChat draft through the existing fixed-IP VPS protocol;
   - render a 1920x1080 explainer video from an agent-authored narration script and material terms.
6. Preserve article publication success if rendering fails; retry only the failed stage.
7. Present the video for explicit approval.
8. Present platform-specific title, body, tags, and immediate-publication targets for explicit approval.
9. Open local Chrome when login or user action is required.
10. Publish independently to Xiaohongshu, Douyin, and Bilibili and record each result.

## Approval invariants

- Never call the production website/WeChat endpoint before article, cover, and publication approval.
- Never publish a video platform before video and distribution approval.
- Never infer approval from artifact existence, a prior run, or an Agent's intention.
- A retry may repeat only the selected failed stage. It must not replay successful external publications.
- Dry-run is the default for commands capable of external mutation.

## Article behavior

- Preserve the existing site payload and fixed-IP VPS route.
- WeChat stops after draft creation. It does not call free-publish.
- WeChat cover handling preserves the proven temporary-media behavior on the VPS.
- Article body contains no inline illustrations. The WeChat cover is required and separately approved.
- Existing duplicate-slug recovery remains available only through explicit authorization.

## Video behavior

- Produce one 16:9 landscape video shared by all three video platforms.
- Default resolution and codec: 1920x1080 H.264 video in an MP4 container.
- Default materials: Pexels plus optional local assets.
- Default narration: Edge TTS `zh-CN-YunxiNeural`.
- Generate subtitles and optionally mix a random track from the local BGM library.
- The first release excludes avatars, lip sync, generative-video models, and a timeline editor.

## Browser and accounts

- Launch the locally installed Chrome, not a bundled browser, by default.
- Use a dedicated non-default Chrome Profile per platform.
- Control Chrome through CDP behind a BrowserDriver seam.
- Show a visible browser for login and publishing. Explain the action and stored login state before opening it.
- Never ask the user to provide a social-platform password to the Agent.
- Agent-browser and Playwright adapters are optional fallbacks.

## Local data

- Store all user-editable secrets in `.local/secrets.toml` as plaintext by explicit user choice.
- Store Chrome profiles under `.local/browser-profiles/`.
- Store each Product under `workspace/<date>-<slug>/`.
- Ignore `.local/` and `workspace/` in Git and redact secrets from every log.
- Approved artifact revisions are immutable; new work creates a new revision.

## Release gate

Version 1.0 requires a real, confirmed end-to-end run proving:

- personal-site publication;
- WeChat draft creation;
- landscape video generation;
- Xiaohongshu publication;
- Douyin publication;
- Bilibili publication;
- independent result tracking and retry;
- no secret, cookie, or token exposure in logs or Git.
