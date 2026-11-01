"""Safe rich text (plan task P6-06.1; REQ-193, 327): hostile fixtures come out inert."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from reflex.compiler import compiler

from reflex_agentlens.components.richtext import rich_text
from reflex_agentlens.views.richtext import URL_TRANSFORM_JS, safe_url

URLS = {
    "https://example.com/a?b=1": "https://example.com/a?b=1",
    "HTTP://EXAMPLE.COM": "HTTP://EXAMPLE.COM",
    "mailto:ana@example.com": "mailto:ana@example.com",
    "javascript:alert(1)": "",
    "JaVaScRiPt:alert(1)": "",
    " javascript:alert(1)": "",
    "java\tscript:alert(1)": "",
    "java\nscript:alert(1)": "",
    "\x00javascript:alert(1)": "",
    "vbscript:msgbox(1)": "",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==": "",
    "data:image/svg+xml,<svg onload=alert(1)>": "",
    "file:///etc/passwd": "",
    "//evil.example/x": "",
    "/agentlens/settings": "",
    "#top": "",
    "": "",
}


@pytest.mark.parametrize(("url", "expected"), URLS.items())
def test_url_policy(url: str, expected: str) -> None:
    assert safe_url(url) == expected


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_browser_policy_matches_the_python_policy() -> None:
    script = f"const f = {URL_TRANSFORM_JS};\nconst cases = {json.dumps(list(URLS))};\n"
    script += "console.log(JSON.stringify(cases.map((u) => f(u, 'href', null))));"
    out = subprocess.run([str(NODE), "-e", script], capture_output=True, text=True, check=True,
                         timeout=30)  # fmt: skip
    assert json.loads(out.stdout) == [safe_url(u) for u in URLS]


def compiled(text: str) -> str:
    return compiler._compile_page(rich_text(text), "x")


def test_raw_html_is_never_parsed() -> None:
    js = compiled("<script>alert(1)</script><img src=x onerror=alert(1)>")
    assert "skipHtml:true" in js
    assert "rehypeRaw" not in js
    assert "rehype-raw" not in js
    assert "rehypePlugins:[]" in js
    assert "remarkMath" not in js  # no KaTeX: no raw HTML from math either


def test_links_and_images_go_through_the_policy() -> None:
    js = compiled("[x](javascript:alert(1)) ![y](https://tracker.example/p.png)")
    assert f"urlTransform:{URL_TRANSFORM_JS}" in js
    assert 'rel:"noopener noreferrer nofollow"' in js
    # an image is a link to its source: no <img> element, so no remote fetch
    img = js[js.index('["img"]') : js.index('["pre"]')]
    assert '"img",' not in img
    assert 'jsx("img"' not in img
    assert "props.src" in img


def test_text_is_a_child_never_markup() -> None:
    payload = '"><script>alert(1)</script>'
    js = compiled(payload)
    assert json.dumps(payload) in js  # a JS string literal, escaped by React at render
