import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline.cli import app
from agent_content_pipeline.workspace import ProductCreateRequest, ProductWorkspace


def png_fixture(width: int, height: int) -> bytes:
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            (13).to_bytes(4, "big"),
            b"IHDR",
            width.to_bytes(4, "big"),
            height.to_bytes(4, "big"),
            b"\x08\x02\x00\x00\x00",
            b"\x00\x00\x00\x00",
            (1).to_bytes(4, "big"),
            b"IDAT",
            b"\x00",
            b"\x00\x00\x00\x00",
            (0).to_bytes(4, "big"),
            b"IEND",
            b"\x00\x00\x00\x00",
        )
    )


def valid_article_files(title: str = "斩我斋：测试文章") -> tuple[str, str, str]:
    mdx = f"""---
title: "{title}"
date: 2026-08-10
category: essay
tags: ["AI", "思考"]
summary: "测试摘要"
---

正文
"""
    style = "font-size: 16px; color: #222; line-height: 1.8; text-align: left"
    body = (
        f'<p style="{style}">{title}</p>'
        f'<p style="{style}">2026.08.10</p>'
        f'<p style="{style}">正文</p>'
    )
    return mdx, body, f"<html><body>{body}</body></html>"


def test_cli_adds_one_immutable_article_revision(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="测试文章",
            slug="artifact-test",
            created_on=date(2026, 8, 10),
        )
    )
    source = tmp_path / "incoming"
    source.mkdir()
    mdx = source / "article.mdx"
    body = source / "body.html"
    wechat = source / "index.html"
    mdx_text, body_text, wechat_text = valid_article_files()
    mdx.write_text(mdx_text, encoding="utf-8")
    body.write_text(body_text, encoding="utf-8")
    wechat.write_text(wechat_text, encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-article",
            "--product",
            str(product.root),
            "--mdx",
            str(mdx),
            "--body-html",
            str(body),
            "--wechat-html",
            str(wechat),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    revision_root = product.root / "article" / "v001"
    assert payload == {
        "ok": True,
        "artifact": {
            "kind": "article",
            "revision": "v001",
            "root": str(revision_root),
        },
    }
    assert (revision_root / "article.mdx").read_text(encoding="utf-8").endswith("正文\n")
    assert (revision_root / "body.html").read_text(encoding="utf-8") == body_text
    assert (revision_root / "index.html").is_file()


def test_cli_adds_a_landscape_png_cover_revision(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="封面测试",
            slug="cover-test",
            created_on=date(2026, 8, 10),
        )
    )
    cover = tmp_path / "cover.png"
    cover.write_bytes(png_fixture(900, 383))

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-cover",
            "--product",
            str(product.root),
            "--cover",
            str(cover),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["artifact"]["kind"] == "cover"
    assert payload["artifact"]["revision"] == "v001"
    assert (product.root / "cover" / "v001" / "cover.png").read_bytes() == cover.read_bytes()


def test_cli_rejects_a_portrait_cover_before_creating_a_revision(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="封面测试",
            slug="portrait-cover-test",
            created_on=date(2026, 8, 10),
        )
    )
    cover = tmp_path / "portrait.png"
    cover.write_bytes(png_fixture(383, 900))

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-cover",
            "--product",
            str(product.root),
            "--cover",
            str(cover),
        ],
    )

    assert result.exit_code == 2
    assert "cover.png must be landscape" in result.output
    assert list((product.root / "cover").iterdir()) == []


def test_cli_rejects_editorial_source_markers_from_a_release_article(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="发行校验",
            slug="release-marker-test",
            created_on=date(2026, 8, 10),
        )
    )
    source = tmp_path / "incoming-markers"
    source.mkdir()
    mdx = source / "article.mdx"
    body = source / "body.html"
    wechat = source / "index.html"
    mdx.write_text(
        "---\ntitle: 斩我斋：发行校验\n---\n正文\n\n原始记录：https://example.com\n",
        encoding="utf-8",
    )
    body.write_text("<p>正文</p>", encoding="utf-8")
    wechat.write_text("<html><body>正文</body></html>", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-article",
            "--product",
            str(product.root),
            "--mdx",
            str(mdx),
            "--body-html",
            str(body),
            "--wechat-html",
            str(wechat),
        ],
    )

    assert result.exit_code == 2
    assert "release article contains internal marker: 原始记录" in result.output
    assert list((product.root / "article").iterdir()) == []


def test_cli_rejects_unapproved_external_links_from_a_release_article(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="链接校验",
            slug="release-link-test",
            created_on=date(2026, 8, 10),
        )
    )
    source = tmp_path / "incoming-link"
    source.mkdir()
    mdx = source / "article.mdx"
    body = source / "body.html"
    wechat = source / "index.html"
    mdx.write_text(
        "---\ntitle: 斩我斋：链接校验\n---\n正文：https://example.com\n",
        encoding="utf-8",
    )
    body.write_text("<p>正文</p>", encoding="utf-8")
    wechat.write_text("<html><body>正文</body></html>", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-article",
            "--product",
            str(product.root),
            "--mdx",
            str(mdx),
            "--body-html",
            str(body),
            "--wechat-html",
            str(wechat),
        ],
    )

    assert result.exit_code == 2
    assert "release article must not contain external links" in result.output
    assert list((product.root / "article").iterdir()) == []
