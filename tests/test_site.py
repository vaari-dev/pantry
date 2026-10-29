import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "site"))
import build


def test_the_site_builds_with_every_link_and_anchor_in_place(tmp_path):
    build.build(tmp_path)
    pages = {p.relative_to(tmp_path).as_posix(): p.read_text() for p in tmp_path.rglob("*.html")}
    assert len(pages) == 1 + len(build.PAGES)
    for name, text in pages.items():
        for href in re.findall(r'href="([^"#:]*)(?:#([^"]*))?"', text):
            target = (Path(name).parent / href[0]).as_posix() if href[0] else name
            target = re.sub(r"[^/]+/\.\./", "", target)
            if target.endswith(".html"):
                assert target in pages, f"{name} links to {target}"
                assert not href[1] or f'id="{href[1]}"' in pages[target], f"{name} links to {target}#{href[1]}"


def test_every_command_is_in_the_reference(tmp_path):
    build.build(tmp_path)
    reference = (tmp_path / "guide" / "reference.html").read_text()
    for command in build.constants("bot")["COMMANDS"]:
        assert f"<b>/{command}</b>" in reference


def test_a_quote_the_code_no_longer_says_is_stale():
    known = build.templates()
    page = (
        '<p class="chatline bot">Basket emptied.<span class="kb">Order now</span></p><p class="bot">Basket deleted.</p>'
    )
    assert build.stale("page", page, known) == ["page: Basket deleted."]


def test_an_unknown_fact_stops_the_build():
    with pytest.raises(SystemExit, match="unknown"):
        build.fill("{{no_such_fact}}", build.facts())
