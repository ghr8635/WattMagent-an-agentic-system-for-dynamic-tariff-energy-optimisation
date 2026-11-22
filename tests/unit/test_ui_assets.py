"""The web page's files: present, and written so a strict Content-Security-Policy accepts them."""

from __future__ import annotations

import re
from importlib import resources

import pytest

FILES = ("index.html", "app.js", "style.css")


def read(name: str) -> str:
    return resources.files("wattmagent.api").joinpath("ui", name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILES)
def test_the_page_files_ship_with_the_package(name: str) -> None:
    assert len(read(name)) > 200


def test_the_page_has_no_inline_script_style_or_handler() -> None:
    html = read("index.html")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)  # every script is a file
    assert "<style" not in html and not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"\son[a-z]+\s*=", html)  # no onclick= and the like


def test_nothing_is_loaded_from_another_host() -> None:
    for name in FILES:
        text = read(name)
        # the SVG namespace is an identifier, not a request
        urls = [u for u in re.findall(r"https?://[^\s\"')]+", text) if "w3.org" not in u]
        assert urls == [], (name, urls)


def test_the_script_never_builds_html_from_data() -> None:
    js = read("app.js")
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "localStorage",
        "sessionStorage",
        "document.cookie",
    ):
        assert banned not in js, banned
    assert "new Function" not in js


def test_the_script_sets_no_inline_style_through_attributes() -> None:
    js = read("app.js")
    assert '"style"' not in js and ".style." not in js and "cssText" not in js


def test_the_page_names_the_limits_of_its_numbers() -> None:
    html = read("index.html")
    assert "planned, not measured" in html and "wholesale only" in html
