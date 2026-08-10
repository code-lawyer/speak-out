# Upstream research record

Reviewed on 2026-08-10.

## MoneyPrinterTurbo

- Repository: https://github.com/harry0703/MoneyPrinterTurbo
- Reviewed main commit: `e14dea5578bb2154480b6bc436009eaa9987f0e3`
- License: MIT
- Reviewed areas: `app/services/voice.py`, `subtitle.py`, `material.py`, `video.py`, and task orchestration.

The new project absorbed the useful pipeline boundaries and failure lessons: file-based Edge TTS with bounded failure, SRT preservation, landscape material selection, clip normalization, duration-driven sequencing, optional BGM degradation, and final media inspection. It implements composition directly through system FFmpeg and does not import MoneyPrinterTurbo, MoviePy, Streamlit, Redis, its task server, or its LLM layer.

## AiToEarn

- Repository: https://github.com/yikart/AiToEarn
- Reviewed main commit: `e8b0bfcce9186b0449b5b20137538394e3bddada`
- License: MIT
- Reviewed areas: Bilibili, Douyin, RedNote, and WeChat publish providers, platform metadata, validation, finalize, verify, user-handoff, and polling behavior.

The new project absorbed provider boundaries, platform-specific validation, independent state, `submitted` versus finalized results, user handoff, and reconciliation rules. It did not absorb AiToEarn Cloud, Relay, OAuth storage, MongoDB/Redis, asset hosting, or its current WeChat path.

Important findings:

- AiToEarn Bilibili uses asynchronous archive submission and later verification.
- AiToEarn Douyin is a user-handoff flow and does not prove immediate public availability at initial submission.
- AiToEarn RedNote's reviewed provider validates an existing work link; it is not a local-file uploader.
- AiToEarn WeChat is incompatible with the maintainer's fixed-IP draft-only requirement.

Therefore the first three local adapters use visible installed Chrome and dedicated Profiles. Their page selectors are isolated contracts and require live verification before the v1 release gate.
