# Speak Out agent instructions

This repository is the durable project memory. At the start of a new session,
read these files in order before changing code or running a publication:

1. `docs/PROJECT_STATUS.md` — current implementation and release state.
2. `docs/PRODUCT.md` — product goals and non-negotiable behavior.
3. `docs/ARCHITECTURE.md` — module boundaries and failure model.
4. `docs/SECURITY.md` — secrets, approvals, snapshots, and browser safety.
5. `.agents/skills/speak-out/SKILL.md` — the Agent-facing production workflow.
6. `docs/RELEASE_EVIDENCE.md` — what has and has not been proven live.

## Durable decisions

- Prioritize the maintainer's real workflow over generic platformization.
- Website publication and WeChat draft creation use the existing fixed-IP VPS
  relay. Do not redesign or modify the VPS unless the user explicitly requests it.
- WeChat stops at the draft box. It does not call free-publish.
- Generate one required WeChat cover and no inline article illustrations.
- New WeChat article revisions use the deterministic `wechat-editorial-v1`
  layout. Final publication content must not retain research/source-note links.
- Produce one 16:9 landscape MP4 for Xiaohongshu, Douyin, and Bilibili.
- Reuse public Pexels clips through `.local/media-cache/`; never substitute a
  local clip loop when remote Pexels acquisition was selected.
- Use visible locally installed Chrome with one dedicated Profile per platform.
  Do not request social-platform passwords from the user.
- Store user-editable credentials only in ignored `.local/secrets.toml` and
  browser login state only in ignored `.local/browser-profiles/`.
- Every irreversible external action requires an exact recorded approval.
  Preserve independent successes; retry only the named safe stage.
- `speak-out` is the primary CLI. `acp` and the Python package path
  `agent_content_pipeline` remain compatibility interfaces.

## Current release boundary

The local/open-source MVP is usable. The website/WeChat route and two real
1920x1080 video renders have succeeded. The offline suite has 122 passing tests.
Version 1.0 is still blocked on a clean-clone end-to-end acceptance run and one
confirmed live publication to each of Xiaohongshu, Douyin, and Bilibili. Never
claim those gates passed without updating `docs/RELEASE_EVIDENCE.md` with direct
destination-confirmed evidence.

## Local-state boundary

Git intentionally excludes `.local/` and `workspace/`. A fresh clone restores
the project knowledge and code, but not credentials, Chrome login sessions,
media cache, Product artifacts, approvals, or publication history. Follow
`docs/CLEAN_CLONE_CHECKLIST.md` before deleting an existing checkout.
