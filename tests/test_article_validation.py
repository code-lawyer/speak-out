from agent_content_pipeline.validation import validate_article_release


def valid_mdx() -> str:
    return """---
title: "斩我斋：一次测试"
date: 2026-08-10
category: essay
tags: ["AI", "思考"]
summary: "一句话摘要"
---

这是正文。
"""


def valid_body() -> str:
    style = "font-size: 16px; color: #222; line-height: 1.8; text-align: left"
    return (
        f'<p style="{style}">斩我斋：一次测试</p>'
        f'<p style="{style}">2026.08.10</p>'
        f'<p style="{style}">这是正文。</p>'
    )


def test_release_validation_accepts_the_proven_article_contract():
    assert validate_article_release(valid_mdx(), valid_body()) == ()


def test_release_validation_preserves_frontmatter_and_style_guardrails():
    mdx = valid_mdx().replace("斩我斋：", "").replace("[\"AI\", \"思考\"]", "[\"其他\"]")
    body = valid_body().replace("text-align: left", "")

    issues = validate_article_release(mdx, body)

    assert 'title must start with "斩我斋："' in issues
    assert "tag is not allowed: 其他" in issues
    assert "paragraph 1 style is missing text-align" in issues


def test_release_validation_rejects_body_images_and_non_paragraph_blocks():
    body = valid_body() + '<div><img src="cover.png"></div>'

    issues = validate_article_release(valid_mdx(), body)

    assert "body.html contains a forbidden block tag" in issues
    assert "article body must not contain images" in issues
    assert "all block content in body.html must be top-level paragraphs" in issues


def test_release_validation_rejects_exclamation_marks_and_wrong_heading_rows():
    mdx = valid_mdx().replace("这是正文。", "这是正文！")
    body = valid_body().replace("斩我斋：一次测试", "错误标题", 1).replace("2026.08.10", "2026-08-10")

    issues = validate_article_release(mdx, body)

    assert "exclamation marks are not allowed" in issues
    assert "first paragraph must exactly match the title" in issues
    assert "second paragraph must be 2026.08.10" in issues
