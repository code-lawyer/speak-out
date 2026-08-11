from agent_content_pipeline.wechat import WeChatArticleRenderer


def test_renderer_turns_article_semantics_into_the_editorial_wechat_layout():
    markdown = """---
title: "斩我斋：确定性排版"
date: 2026-08-11
category: essay
tags: ["AI", "思考"]
summary: "验证公众号排版不会退化为普通文字。"
---

开场正文包含**重点判断**。

## 第一部分

这是章节正文。

> 这是一段需要突出显示的引用。

- 第一项
- 第二项
"""

    rendered = WeChatArticleRenderer().render(markdown)

    assert rendered.body_html.startswith(
        '<p style="font-family:&quot;PingFang SC&quot;,&quot;Hiragino Sans GB&quot;,'
        '&quot;Microsoft YaHei&quot;,sans-serif;font-size:21px;font-weight:700;'
        'color:#1a1a1a;line-height:1.4;text-align:center;margin:0 0 6px;">'
        "斩我斋：确定性排版</p>"
    )
    assert ">2026.08.11</p>" in rendered.body_html
    assert "border-left:3px solid #37475a;\">一</p>" in rendered.body_html
    assert "第一部分</p>" in rendered.body_html
    assert "background:#f3f5f6" in rendered.body_html
    assert ">• 第一项</p>" in rendered.body_html
    assert (
        '<strong style="color:#1a1a1a;font-weight:700;">重点判断</strong>'
        in rendered.body_html
    )
    assert "<h2" not in rendered.body_html
    assert "<div" not in rendered.body_html
    assert rendered.body_html in rendered.preview_html
