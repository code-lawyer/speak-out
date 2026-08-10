from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any


RELEASE_INTERNAL_MARKERS = ("原始记录", "原文出处", "研究笔记", "供参考")
ALLOWED_ARTICLE_TAGS = frozenset(("法律", "AI", "Web3", "思考", "时代"))


@dataclass(frozen=True)
class ParsedArticle:
    frontmatter: dict[str, Any]
    body: str


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_article_mdx(markdown: str) -> ParsedArticle:
    normalized = markdown.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    match = re.fullmatch(r"---\n([\s\S]*?)\n---\n([\s\S]*)", normalized)
    if match is None:
        raise ValueError("frontmatter missing or malformed")

    data: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith("[") and raw_value.endswith("]"):
            inner = raw_value[1:-1].strip()
            data[key] = (
                [_unquote(item.strip()) for item in inner.split(",")]
                if inner
                else []
            )
        else:
            data[key] = _unquote(raw_value)
    return ParsedArticle(frontmatter=data, body=match.group(2))


def validate_release_markers(*documents: str) -> tuple[str, ...]:
    joined = "\n".join(documents)
    return tuple(
        f"release article contains internal marker: {marker}"
        for marker in RELEASE_INTERNAL_MARKERS
        if marker in joined
    )


def validate_release_links(markdown: str, *rendered_documents: str) -> tuple[str, ...]:
    allows_links = bool(
        re.search(r"(?m)^\s*allowLinks\s*:\s*['\"]?true['\"]?\s*$", markdown)
    )
    if allows_links:
        return ()
    if re.search(r"https?://", "\n".join((markdown, *rendered_documents)), re.IGNORECASE):
        return ("release article must not contain external links",)
    return ()


def _plain_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).replace("\xa0", " ").strip()


def validate_article_release(markdown: str, wechat_body: str) -> tuple[str, ...]:
    """Validate the release contract proven by the original article Skill."""

    try:
        article = parse_article_mdx(markdown)
    except ValueError as error:
        return (str(error),)

    data = article.frontmatter
    issues: list[str] = []
    title = data.get("title")
    article_date = data.get("date")
    tags = data.get("tags")
    allow_links = data.get("allowLinks") == "true"

    if not isinstance(title, str) or not title.startswith("斩我斋："):
        issues.append('title must start with "斩我斋："')
    if not isinstance(article_date, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", article_date) is None:
        issues.append("date must use YYYY-MM-DD")
    if data.get("category") != "essay":
        issues.append('category must be "essay"')
    if not isinstance(tags, list) or not 1 <= len(tags) <= 3:
        issues.append("tags must contain 1-3 values")
    else:
        issues.extend(f"tag is not allowed: {tag}" for tag in tags if tag not in ALLOWED_ARTICLE_TAGS)
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        issues.append("summary is required")
    if not article.body.strip():
        issues.append("article body is empty")
    if re.search(r"[!！]", article.body) or (isinstance(title, str) and re.search(r"[!！]", title)):
        issues.append("exclamation marks are not allowed")
    if not allow_links and re.search(r"(?:\[[^\]]+\]\(https?://[^)]+\)|https?://)", article.body, re.IGNORECASE):
        issues.append("release article must not contain external links unless frontmatter sets allowLinks: true")

    if len(wechat_body) > 20_000:
        issues.append("body.html exceeds 20,000 characters")
    if re.search(r"</?(?:h[1-6]|div|section|style)\b", wechat_body, re.IGNORECASE):
        issues.append("body.html contains a forbidden block tag")
    if re.search(r"\sclass\s*=", wechat_body, re.IGNORECASE):
        issues.append("body.html must not use class attributes")
    if re.search(r"<img\b", wechat_body, re.IGNORECASE):
        issues.append("article body must not contain images")
    if not allow_links and re.search(r"(?:<a\b|https?://)", wechat_body, re.IGNORECASE):
        issues.append("body.html must not contain external links unless frontmatter sets allowLinks: true")

    paragraphs = list(re.finditer(r"<p\b([^>]*)>([\s\S]*?)</p>", wechat_body, re.IGNORECASE))
    if len(paragraphs) < 2:
        issues.append("body.html must contain at least title and date paragraphs")
        return tuple(issues)

    if re.sub(r"<p\b[^>]*>[\s\S]*?</p>", "", wechat_body, flags=re.IGNORECASE).strip():
        issues.append("all block content in body.html must be top-level paragraphs")

    required_styles = ("font-size", "color", "line-height", "text-align")
    for index, paragraph in enumerate(paragraphs, start=1):
        style_match = re.search(r"\sstyle\s*=\s*([\"'])([\s\S]*?)\1", paragraph.group(1), re.IGNORECASE)
        if style_match is None:
            issues.append(f"paragraph {index} is missing an inline style")
            continue
        style = style_match.group(2)
        for property_name in required_styles:
            if re.search(rf"(?:^|;)\s*{re.escape(property_name)}\s*:", style, re.IGNORECASE) is None:
                issues.append(f"paragraph {index} style is missing {property_name}")

    if _plain_text(paragraphs[0].group(2)) != title:
        issues.append("first paragraph must exactly match the title")
    if isinstance(article_date, str):
        expected_date = article_date.replace("-", ".")
        if _plain_text(paragraphs[1].group(2)) != expected_date:
            issues.append(f"second paragraph must be {expected_date}")

    return tuple(issues)


def validate_png_cover(buffer: bytes) -> tuple[str, ...]:
    issues: list[str] = []
    signature = bytes((137, 80, 78, 71, 13, 10, 26, 10))
    if len(buffer) < 33 or buffer[:8] != signature:
        return ("cover.png is not a valid PNG file",)

    ihdr_length = int.from_bytes(buffer[8:12], "big")
    ihdr_type = buffer[12:16]
    if ihdr_length != 13 or ihdr_type != b"IHDR":
        return ("cover.png is missing a valid IHDR chunk",)

    width = int.from_bytes(buffer[16:20], "big")
    height = int.from_bytes(buffer[20:24], "big")
    if width == 0 or height == 0:
        issues.append("cover.png dimensions must be non-zero")
    if width <= height:
        issues.append(f"cover.png must be landscape; received {width}x{height}")

    offset = 8
    saw_image_data = False
    saw_end = False
    while offset + 12 <= len(buffer):
        length = int.from_bytes(buffer[offset : offset + 4], "big")
        type_start = offset + 4
        data_start = offset + 8
        next_offset = data_start + length + 4
        if next_offset > len(buffer):
            issues.append("cover.png contains a truncated chunk")
            break
        chunk_type = buffer[type_start : type_start + 4]
        if chunk_type == b"IDAT":
            saw_image_data = True
        if chunk_type == b"IEND":
            saw_end = True
            break
        offset = next_offset

    if not saw_image_data:
        issues.append("cover.png is missing image data")
    if not saw_end:
        issues.append("cover.png is missing the end chunk")
    return tuple(issues)
