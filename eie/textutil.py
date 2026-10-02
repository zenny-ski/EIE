import html
import re
from html.parser import HTMLParser


class _Stripper(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    stripper = _Stripper()
    stripper.feed(markup)
    text = html.unescape("".join(stripper.parts))
    return normalize(text)


# Invisible characters marketing emails use to pad the inbox preview line
# (zero-width spaces/joiners, BOM, soft hyphen, combining grapheme joiner, etc.).
_INVISIBLE = re.compile("[­͏\u061Cᅟᅠ឴឵᠎​-\u200F\u202A-\u202E"
                        "⁠-⁤⁪-⁯ㅤ﻿ﾠ]")


def normalize(text: str) -> str:
    text = _INVISIBLE.sub("", text)
    text = text.replace("\r\n", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()
