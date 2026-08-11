# Project status

Last updated: 2026-08-11  
Repository: `https://github.com/code-lawyer/speak-out`  
Current baseline commit when this status was prepared: `b3eb103`

## Executive status

Speak Out has reached a usable open-source MVP. Article preparation, fixed-IP
VPS website/WeChat publication, deterministic WeChat layout, 16:9 video
rendering, reusable Pexels caching, approval/state management, and visible local
Chrome adapters are implemented. Version 1.0 is not yet declared because the
clean-clone release run and three live social-platform submissions remain open.

## Implemented

| Area | Current state |
| --- | --- |
| Product workspace | Versioned, SHA-256-sealed artifacts with exact approvals |
| Article output | MDX, deterministic WeChat HTML, required approved cover |
| Article publication | Existing fixed-IP VPS contract; website plus WeChat draft |
| Video engine | Edge TTS or local narration, SRT, Pexels/local materials, BGM, FFmpeg, FFprobe |
| Media reuse | Project-local Pexels cache with sealed provenance and attribution checks |
| Browser foundation | Visible installed Chrome 116+, dedicated Profiles, CDP, owned-process cleanup |
| Social adapters | Xiaohongshu, Douyin, Bilibili offline contracts and exact metadata read-back |
| Orchestration | Dry-run planning, approval gates, independent stages, idempotency, retry, reconciliation |
| Agent interface | Project-local `$speak-out` Skill with progressive references |
| Compatibility | `acp` CLI alias and `agent_content_pipeline` import path retained |

## Proven on the maintainer machine

- `speak-out doctor` passed Python 3.12, FFmpeg, FFprobe, configuration,
  `.local/` Git isolation, and Chrome 151.
- The offline suite passed 122 tests on 2026-08-11.
- Product `ai-is-coming-for-lawyers` recorded `website-wechat = succeeded`;
  the user confirmed the personal-site article and WeChat draft were received.
- The same Product produced two inspected 1920x1080 H.264 MP4 revisions with
  audio and 495.1-second duration:
  - `v001`: approved local material revision plus Edge TTS;
  - `v002`: 24 remotely acquired Pexels clips plus Edge TTS.
- The official Skill validator passed `.agents/skills/speak-out`.
- The public repository exists and `main` is synchronized with the local commit.

The Product, logs, secrets, downloaded media, and browser state supporting the
live evidence are deliberately ignored by Git. `docs/RELEASE_EVIDENCE.md` is the
portable record; local files are supporting evidence only.

## Remaining v1.0 work

1. Clone the public repository into a clean directory and run setup, doctor,
   tests, Skill validation, and a new Product from scratch.
2. Re-prove website publication and WeChat draft creation from that fresh clone.
3. Render and inspect a new approved 16:9 video from the fresh Product.
4. Approve and publish that exact video separately to Xiaohongshu, Douyin, and
   Bilibili through visible dedicated Chrome Profiles.
5. Exercise one real safe retry or reconciliation path.
6. Audit Git state, logs, screenshots, and terminal output for credential leaks.
7. Update `docs/RELEASE_EVIDENCE.md`; only then tag version 1.0.

## Known operational facts

- Local absolute paths saved in historical ignored Product ledgers may contain
  the former directory name `agent-content-pipeline`. Do not copy those commands
  into a new checkout; create a new Product for the clean-clone acceptance run.
- Social creator pages are external SPAs. Selector contracts are tested offline,
  but live page changes can still require an Adapter update.
- The fixed-IP VPS is an external dependency owned by the maintainer. Keep its
  current contract stable unless the user explicitly authorizes a server change.
