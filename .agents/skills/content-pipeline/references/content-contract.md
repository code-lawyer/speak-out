# Content artifact contract

## Product rule

Store all files for one content Product inside `workspace/<date>-<slug>/`. Do not overwrite an approved revision; create the next revision.

## Required written artifacts

- Source notes under `source/`.
- Website MDX with valid frontmatter under `article/<revision>/`.
- WeChat body and complete HTML under the same article revision.
- Approved landscape PNG cover under `cover/<revision>/`.

The public release article must not contain internal labels such as “原始记录”, “原文出处”, “研究笔记”, or “供参考”. Do not include external links unless the user explicitly approves links for that article.

Store those raw notes and sources in an immutable `source/vNNN` revision instead. Every artifact revision includes a generated `.artifact.json`; do not edit it or any sealed file.

The WeChat body contains text paragraphs only. Do not add inline article images.

## Required video artifacts

- Spoken narration script under `video/script/<revision>/`.
- Search terms as `materialTerms` beside the complete `narration` in `script.json`.
- Downloaded or local materials under `video/materials/<revision>/` or a render revision's `materials/` directory.
- Narration, subtitle, and optional BGM intermediates under their named directories.
- Final shared 1920x1080 MP4 under `video/renders/<revision>/`.
- Platform copy bundle under `publish/copy/<revision>/copy.json`.

Use a spoken script, not raw MDX. Do not read headings, URLs, citations, frontmatter, or formatting tokens aloud.

## Platform metadata

Prepare title, body, topics/tags, and required platform options separately for Xiaohongshu, Douyin, and Bilibili. Keep the thesis and factual claims consistent across platforms.

Current local guardrails are: Xiaohongshu title 20/body 1000/up to 5 tags; Douyin title 30/body 1000/up to 5 tags; Bilibili title 79/body 249 and a required category. Treat these as preflight bounds, not a guarantee that the live page has not changed.
