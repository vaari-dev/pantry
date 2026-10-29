"""Builds the site into site/_site: python3 site/build.py

home.html and guide/*.html hold each page's own content; the header, guide navigation, page contents
and footer are added here, so every page shares one navigation.

Nothing about the bot is copied by hand: {{name}} takes a number or list from the bot's source (FACTS),
and every element with class "bot" quotes the bot, line by line, as its code words it. Either going
out of date fails the build, so the site can't say what the bot no longer does.
"""

import ast
import contextlib
import html
import re
import shutil
import sys
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).parent
OUT = SITE / "_site"
CODE = SITE.parent / "src" / "pantry"
URL = "https://vaari-dev.github.io/pantry/"
BOT = "joey_butler_bot"
ASSETS = ("style.css", "favicon.svg", "og.png")

GUIDE = [  # (section, [(page, title, one-line summary)])
    ("Start", [
        ("index", "Welcome", "What Pantry does, and where to find everything"),
        ("getting-started", "Join and connect", "Get in, connect your Zepto account, place a first order"),
    ]),
    ("Order", [
        ("asking", "Ask for groceries", "Voice or text, Hinglish, counts and amounts"),
        ("products", "Choose your products", "Pick once from photos; Pantry remembers"),
        ("basket", "Your basket", "The minimum, the 3-minute wait, Order now, changes"),
        ("delivery", "Delivery and payment", "Your address, another one for one order, how you pay"),
        ("after", "After you order", "What the ✅ means, and what to do when it's missing"),
    ]),
    ("Family and settings", [
        ("family", "Order as a family", "Invite people, owner and members, leaving"),
        ("settings", "Settings and limits", "Daily limit, smallest order, cart-only mode"),
    ]),
    ("Trust", [
        ("safety", "How your money stays safe", "Every bill checked; never ordered twice"),
        ("privacy", "Privacy and your data", "What Pantry keeps, who sees what, deleting it"),
    ]),
    ("Help", [
        ("trouble", "When something goes wrong", "What Pantry does when an order can't go through, and what to do"),
        ("reference", "Everything you can say", "Every command and everyday phrase"),
        ("faq", "Questions", "Short answers to common questions"),
    ]),
]  # fmt: skip
PAGES = [(section, *page) for section, pages in GUIDE for page in pages]

TELEGRAM_ICON = (
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M21.9 4.3 18.7 19.4c-.2 1-.9 1.3-1.8.8'
    "l-4.8-3.6-2.3 2.2c-.3.3-.5.5-1 .5l.3-4.9 8.9-8c.4-.3-.1-.5-.6-.2L6.4 13.1l-4.7-1.5c-1-.3-1-1 .2-1.5L20.5 3c.9-.3"
    ' 1.6.2 1.4 1.3z"/></svg>'
)


def shell(title: str, description: str, body: str, root: str, path: str) -> str:
    links = [("guide/index.html", "Guide"), ("guide/safety.html", "Safety"), ("guide/privacy.html", "Privacy"),
             ("guide/faq.html", "Questions")]  # fmt: skip
    nav = "".join(f'<li><a href="{root}{href}">{label}</a></li>' for href, label in links)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(description)}">
<meta property="og:type" content="website">
<meta property="og:image" content="{URL}og.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#fbf8f1" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0c130f" media="(prefers-color-scheme: dark)">
<meta name="referrer" content="no-referrer">
<link rel="canonical" href="{URL}{path}">
<link rel="icon" href="{root}favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{root}style.css">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="nav">
  <nav class="wrap" aria-label="Main">
    <a class="brand" href="{root}index.html"><img src="{root}favicon.svg" alt="" width="28" height="28">Pantry</a>
    <ul>{nav}</ul>
    <a class="btn primary small" href="https://t.me/{BOT}">Start on Telegram</a>
  </nav>
</header>
{body}
<footer>
  <div class="wrap">
    <span>Pantry · groceries by voice · made for families in India</span>
    <span><a href="{root}guide/index.html">Guide</a> · <a href="{root}guide/privacy.html">Privacy</a>
      · Not affiliated with Zepto</span>
  </div>
</footer>
</body>
</html>
"""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", text).lower()).strip("-")


def with_ids(content: str) -> tuple[str, list[tuple[str, str]]]:
    """Gives every <h2> an id; returns the content and its (id, title) list."""
    headings = []

    def name(m: re.Match) -> str:
        title = m.group(1)
        headings.append((slug(title), re.sub(r"<[^>]+>", "", title)))
        return f'<h2 id="{headings[-1][0]}">{title}</h2>'

    return re.sub(r"<h2>(.*?)</h2>", name, content), headings


def guide_page(n: int, content: str) -> str:
    section, page, title, summary = PAGES[n]
    content, headings = with_ids(content)
    side = "".join(
        f"<h4>{name}</h4><ul>"
        + "".join(
            f'<li><a href="{p}.html"{' aria-current="page"' if p == page else ""}>{t}</a></li>' for p, t, _ in pages
        )
        + "</ul>"
        for name, pages in GUIDE
    )
    toc = "".join(f'<li><a href="#{i}">{t}</a></li>' for i, t in headings)
    pager = ""
    if n:
        pager += f'<a class="prev" href="{PAGES[n - 1][1]}.html"><small>Previous</small><b>{PAGES[n - 1][2]}</b></a>'
    if n + 1 < len(PAGES):
        pager += f'<a class="next" href="{PAGES[n + 1][1]}.html"><small>Next</small><b>{PAGES[n + 1][2]}</b></a>'
    body = f"""<main id="main" class="wrap doc">
  <aside class="side-nav" aria-label="Guide"><details open><summary>Guide contents</summary>{side}</details></aside>
  <article class="prose">
    <p class="crumb">{section}</p>
    <h1>{title}</h1>
    {content}
    <nav class="pager" aria-label="Previous and next">{pager}</nav>
  </article>
  <nav class="toc" aria-label="On this page">{"<p>On this page</p><ul>" + toc + "</ul>" if toc else ""}</nav>
</main>"""
    return shell(f"{title} · Pantry guide", summary, body, "../", f"guide/{page}.html")


def constants(module: str) -> dict:
    """A module's top-level constants: literals, and timedelta(...) of literals."""
    found = {}
    for node in ast.parse((CODE / f"{module}.py").read_text()).body:
        if not isinstance(node, ast.Assign):
            continue
        for target, value in _pairs(node.targets[0], node.value):
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "timedelta":
                found[target] = timedelta(**{k.arg: ast.literal_eval(k.value) for k in value.keywords})
            elif isinstance(value, ast.Dict) and target == "SETTINGS":  # command: (setting, parse, example)
                found[target] = {
                    ast.literal_eval(k): ast.literal_eval(v.elts[2])
                    for k, v in zip(value.keys, value.values, strict=True)
                }
            else:
                with contextlib.suppress(ValueError):
                    found[target] = ast.literal_eval(value)
    return found


def _pairs(target: ast.expr, value: ast.expr):
    if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
        yield from ((t.id, v) for t, v in zip(target.elts, value.elts, strict=True))
    elif isinstance(target, ast.Name):
        yield target.id, value


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:g} minute{'s' * (seconds != 60)}"


def _rupees(value: float) -> str:
    return f"₹{value:,.0f}"


def facts() -> dict[str, str]:
    bot, accounts, fill_, intent = (constants(m) for m in ("bot", "accounts", "fill", "intent"))
    commands = {f"/{c}": d for c, d in bot["COMMANDS"].items()} | {
        f"{c} {example}": "Change a setting (owner only)" for c, example in bot["SETTINGS"].items()
    }
    rows = "".join(f"<tr><td><b>{html.escape(c)}</b></td><td>{html.escape(d)}</td></tr>" for c, d in commands.items())
    said = re.findall(r'^"(.+)" -> (?!nothing)', intent["PROMPT"], re.M)
    return {
        "BOT": f"https://t.me/{BOT}",
        "HANDLE": f"@{BOT}",
        "ICON": TELEGRAM_ICON,
        "hold": _minutes(bot["HOLD"]),
        "pick_wait": _minutes(bot["PICK_WAIT"]),
        "code_wait": _minutes(bot["OTP_WAIT"]),
        "cards": str(bot["CARDS"]),
        "min_order": _rupees(accounts["DEFAULTS"]["min_order"]),
        "daily_cap": _rupees(accounts["DEFAULTS"]["daily_cap"]),
        "invite_valid": f"{accounts['INVITE_VALID'] / timedelta(hours=1):g} hours",
        "max_packs": str(fill_["MAX_PACKS"]),
        "commands": f"<table><thead><tr><th>Send</th><th>What it does</th></tr></thead><tbody>{rows}</tbody></table>",
        "phrases": '<ul class="phrases">' + "".join(f"<li>“{html.escape(s)}”</li>" for s in said) + "</ul>",
    }


def templates() -> list[re.Pattern]:
    """Each line of every string in the bot's code, its {fields} matching anything; not lines of fields alone."""
    lines = []
    for path in CODE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                parts = [node.value]
            elif isinstance(node, ast.JoinedStr):
                parts = [v.value if isinstance(v, ast.Constant) else None for v in node.values]
            else:
                continue
            line, said = "", ""
            for part in parts:
                for n, piece in enumerate([None] if part is None else part.split("\n")):
                    if n:
                        lines.append((line, said))
                        line, said = "", ""
                    line += ".+?" if piece is None else re.escape(_plain(piece))
                    said += piece or ""
            lines.append((line, said))
    return [re.compile(line) for line in {line for line, said in lines if said.strip()}]


def _plain(text: str) -> str:
    return text.translate(str.maketrans("‘’“”", "''\"\""))


class Quotes(HTMLParser):
    """The text of each element with class "bot", without its buttons (class "kb")."""

    VOID = frozenset({"br", "img", "input", "meta", "link", "path", "rect"})

    def __init__(self):
        super().__init__()
        self.quotes, self.open = [], []  # open: (is a quote, is a button) per open element

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        classes = (dict(attrs).get("class") or "").split()
        self.open.append(("bot" in classes, "kb" in classes))
        if "bot" in classes:
            self.quotes.append("")

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.open:
            self.open.pop()

    def handle_data(self, data):
        if any(q for q, _ in self.open) and not any(b for _, b in self.open):
            self.quotes[-1] += data


def stale(page: str, text: str, known: list[re.Pattern]) -> list[str]:
    """The lines quoted from the bot on a page that no message in its code can say."""
    parser = Quotes()
    parser.feed(text)
    lines = [_plain(line).strip() for quote in parser.quotes for line in quote.split("\n")]
    return [f"{page}: {line}" for line in lines if line and not any(t.fullmatch(line) for t in known)]


def fill(text: str, values: dict[str, str]) -> str:
    def value(m: re.Match) -> str:
        if m.group(1) not in values:
            raise SystemExit(f"unknown {{{{{m.group(1)}}}}}")
        return values[m.group(1)]

    return re.sub(r"\{\{(\w+)\}\}", value, text)


def build(out: Path = OUT) -> None:
    values, known, problems = facts(), templates(), []
    pages = {"index.html": fill((SITE / "home.html").read_text(), values)}
    for page in (p for _, p, *_ in PAGES):
        pages[f"guide/{page}.html"] = fill((SITE / "guide" / f"{page}.html").read_text(), values)
    for page, text in pages.items():
        problems += stale(page, text, known)
    if problems:
        raise SystemExit("quoted from the bot, but no longer in its code:\n" + "\n".join(problems))
    shutil.rmtree(out, ignore_errors=True)
    (out / "guide").mkdir(parents=True)
    for asset in ASSETS:
        shutil.copy(SITE / asset, out / asset)
    title = "Pantry · Groceries from a voice note"
    description = "Say what you need on Telegram. Pantry orders it from Zepto, checks the bill, and never spends more than you allow."  # noqa: E501
    (out / "index.html").write_text(shell(title, description, pages["index.html"], "", ""))
    for n, (_, page, *_) in enumerate(PAGES):
        (out / "guide" / f"{page}.html").write_text(guide_page(n, pages[f"guide/{page}.html"]))


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT)
