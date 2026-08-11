# Clean clone checklist

Use this checklist before deleting or replacing a local checkout.

## What Git preserves

- Source code, tests, documentation, architecture, and release status.
- The project-local `.agents/skills/speak-out` Skill.
- Configuration templates and dependency locks.

## What Git intentionally does not preserve

| Path | Contents | Action before cleanup |
| --- | --- | --- |
| `.local/secrets.toml` | VPS bearer token, Pexels key, editable local settings | Back up securely or re-enter after cloning |
| `.local/browser-profiles/` | Chrome login sessions | Back up only if retaining sessions; otherwise log in again |
| `.local/media-cache/` | Reusable public Pexels clips | Optional backup; otherwise re-download or re-import |
| `workspace/` | Products, approvals, renders, logs, publication state | Archive if historical Products must be retained |
| `.venv/`, `.pytest_cache/`, `dist/` | Rebuildable development output | Safe to discard |

Never commit the ignored directories merely to preserve them. They may contain
credentials, private drafts, account-page screenshots, or large media files.

## Fresh-clone acceptance sequence

```powershell
git clone https://github.com/code-lawyer/speak-out.git
cd speak-out
uv sync --dev
uv run speak-out doctor --project-root . --json
uv run pytest -q
uv run speak-out --help
```

Validate the project Skill with the current Codex `skill-creator`
`quick_validate.py`, supplying PyYAML temporarily if the environment lacks it.
Then read `AGENTS.md`, `docs/PROJECT_STATUS.md`, and
`.agents/skills/speak-out/SKILL.md` before creating the acceptance Product.

For a true clean-clone release test, do not restore an old `workspace/` first.
Create a new Product and execute the full release gate in
`docs/RELEASE_EVIDENCE.md`. Restore archived historical Products only after the
fresh-clone behavior has been proven.
