from __future__ import annotations

import html
import re

from pydantic import BaseModel, ConfigDict

from .validation import parse_article_mdx


class WeChatRenderResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    body_html: str
    preview_html: str
    profile: str = "wechat-editorial-v1"


class WeChatRenderError(ValueError):
    pass


class WeChatArticleRenderer:
    """Render approved MDX into one deterministic, WeChat-safe editorial layout."""

    PROFILE = "wechat-editorial-v1"
    _FONT = (
        "font-family:&quot;PingFang SC&quot;,&quot;Hiragino Sans GB&quot;,"
        "&quot;Microsoft YaHei&quot;,sans-serif;"
    )
    _TITLE = (
        _FONT
        + "font-size:21px;font-weight:700;color:#1a1a1a;line-height:1.4;"
        "text-align:center;margin:0 0 6px;"
    )
    _DATE = (
        _FONT
        + "font-size:12px;color:#9a9fa6;line-height:1.6;text-align:center;"
        "margin:0 0 24px;"
    )
    _BODY = (
        _FONT
        + "font-size:16px;color:#1a1a1a;line-height:1.85;text-align:justify;"
        "margin:12px 0;word-break:break-word;"
    )
    _SECTION_NUMBER = (
        _FONT
        + "font-size:14px;font-weight:700;color:#37475a;line-height:1.5;"
        "text-align:left;margin:36px 0 0;letter-spacing:1px;padding-left:10px;"
        "border-left:3px solid #37475a;"
    )
    _SECTION_TITLE = (
        _FONT
        + "font-size:17px;font-weight:700;color:#1a1a1a;line-height:1.5;"
        "text-align:left;margin:6px 0 12px;padding-left:10px;"
        "border-left:3px solid #37475a;"
    )
    _QUOTE = (
        _FONT
        + "font-size:15px;color:#4a4a4a;line-height:1.8;text-align:left;"
        "background:#f3f5f6;padding:12px 16px;margin:14px 0;"
        "border-radius:0 6px 6px 0;border-left:4px solid #37475a;"
    )
    _LIST = (
        _FONT
        + "font-size:16px;color:#1a1a1a;line-height:1.85;text-align:left;"
        "margin:6px 0;padding-left:16px;"
    )

    def render(self, markdown: str) -> WeChatRenderResult:
        article = parse_article_mdx(markdown)
        title = article.frontmatter.get("title")
        article_date = article.frontmatter.get("date")
        if not isinstance(title, str) or not title:
            raise WeChatRenderError("article title is required")
        if not isinstance(article_date, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", article_date
        ):
            raise WeChatRenderError("article date must use YYYY-MM-DD")
        allow_links = article.frontmatter.get("allowLinks") == "true"

        paragraphs = [
            self._paragraph(self._TITLE, html.escape(title)),
            self._paragraph(self._DATE, article_date.replace("-", ".")),
        ]
        paragraphs.extend(self._render_blocks(article.body, allow_links=allow_links))
        body_html = "\n".join(paragraphs) + "\n"
        preview_html = (
            "<!doctype html>\n"
            '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f"<title>{html.escape(title)}</title>\n</head>\n"
            '<body style="max-width:677px;margin:0 auto;padding:24px 16px;'
            'background:#ffffff;">\n'
            f"{body_html}</body>\n</html>\n"
        )
        return WeChatRenderResult(
            body_html=body_html,
            preview_html=preview_html,
            profile=self.PROFILE,
        )

    def _render_blocks(self, body: str, *, allow_links: bool) -> list[str]:
        lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        rendered: list[str] = []
        section_number = 0
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue
            heading = re.match(r"^#{2,6}\s+(.+?)\s*$", line)
            if heading:
                section_number += 1
                rendered.append(
                    self._paragraph(
                        self._SECTION_NUMBER,
                        self._chinese_number(section_number),
                    )
                )
                rendered.append(
                    self._paragraph(
                        self._SECTION_TITLE,
                        self._inline(heading.group(1), allow_links=allow_links),
                    )
                )
                index += 1
                continue
            if line.startswith("# "):
                raise WeChatRenderError("article body must not contain a level-one heading")
            if line.startswith(">"):
                quote_lines: list[str] = []
                while index < len(lines) and lines[index].strip().startswith(">"):
                    quote_lines.append(lines[index].strip().removeprefix(">").strip())
                    index += 1
                rendered.append(
                    self._paragraph(
                        self._QUOTE,
                        "<br>".join(
                            self._inline(item, allow_links=allow_links)
                            for item in quote_lines
                        ),
                    )
                )
                continue
            list_item = re.match(r"^(?:[-*+]\s+|(\d+)[.)]\s+)(.+)$", line)
            if list_item:
                prefix = f"{list_item.group(1)}. " if list_item.group(1) else "• "
                rendered.append(
                    self._paragraph(
                        self._LIST,
                        prefix
                        + self._inline(list_item.group(2), allow_links=allow_links),
                    )
                )
                index += 1
                continue
            paragraph_lines = [line]
            index += 1
            while index < len(lines):
                candidate = lines[index].strip()
                if not candidate or re.match(
                    r"^(?:#{1,6}\s+|>|[-*+]\s+|\d+[.)]\s+)", candidate
                ):
                    break
                paragraph_lines.append(candidate)
                index += 1
            rendered.append(
                self._paragraph(
                    self._BODY,
                    self._inline("".join(paragraph_lines), allow_links=allow_links),
                )
            )
        return rendered

    @staticmethod
    def _paragraph(style: str, content: str) -> str:
        return f'<p style="{style}">{content}</p>'

    @staticmethod
    def _inline(value: str, *, allow_links: bool) -> str:
        token = re.compile(r"\*\*(.+?)\*\*|\[([^]]+)]\((https?://[^)]+)\)")
        parts: list[str] = []
        cursor = 0
        for match in token.finditer(value):
            parts.append(html.escape(value[cursor : match.start()]))
            if match.group(1) is not None:
                parts.append(
                    '<strong style="color:#1a1a1a;font-weight:700;">'
                    + html.escape(match.group(1))
                    + "</strong>"
                )
            elif allow_links:
                parts.append(
                    f'<a href="{html.escape(match.group(3), quote=True)}" '
                    'style="color:#5a7a9a;text-decoration:underline;">'
                    + html.escape(match.group(2))
                    + "</a>"
                )
            else:
                parts.append(html.escape(match.group(2)))
            cursor = match.end()
        parts.append(html.escape(value[cursor:]))
        return "".join(parts)

    @staticmethod
    def _chinese_number(value: int) -> str:
        digits = "零一二三四五六七八九"
        if value < 10:
            return digits[value]
        if value < 20:
            return "十" + (digits[value % 10] if value % 10 else "")
        if value < 100:
            remainder = digits[value % 10] if value % 10 else ""
            return digits[value // 10] + "十" + remainder
        return str(value)
