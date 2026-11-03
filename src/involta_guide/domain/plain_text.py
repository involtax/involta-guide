"""Plain editorial text with citations retained separately as source URLs."""

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit


def _safe_source(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not (parsed.username or parsed.password)
            and not any(character.isspace() for character in url)
        )
    except ValueError:
        return False


class _PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        elif not self.hidden:
            if tag == "a":
                href = dict(attrs).get("href")
                if href:
                    self.links.append(href)
            if tag in ("br", "p", "div", "li"):
                self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in ("p", "div", "li"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def clean_editorial_text(value: str) -> tuple[str, list[str], list[str]]:
    sources: list[str] = []
    rejected: list[str] = []
    if re.search(r"</?[A-Za-z][^>]*>", value):
        parser = _PlainHTML()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
        for url in parser.links:
            target = sources if _safe_source(url) else rejected
            if url not in target:
                target.append(url)
    result: list[str] = []
    cursor = 0
    while cursor < len(value):
        start = value.find("[", cursor)
        if start < 0:
            result.append(value[cursor:])
            break
        label_end = value.find("]", start + 1)
        if label_end < 0:
            result.append(value[cursor:])
            break
        if value[label_end : label_end + 2] != "](":
            result.append(value[cursor : start + 1])
            cursor = start + 1
            continue
        end = label_end + 2
        depth = 1
        while end < len(value) and depth:
            if value[end] == "(" and (end == 0 or value[end - 1] != "\\"):
                depth += 1
            elif value[end] == ")" and (end == 0 or value[end - 1] != "\\"):
                depth -= 1
            end += 1
        if depth:
            result.append(value[cursor:])
            break
        label = value[start + 1 : label_end]
        raw_url = value[label_end + 2 : end - 1].strip()
        # A Markdown title is presentation, not part of the URL.
        url = raw_url.split(' "', 1)[0].strip("<>")
        safe = _safe_source(url)
        target = sources if safe else rejected
        if url not in target:
            target.append(url)
        before = value[cursor:start]
        # Parenthesised citations are removed as a unit. Inline prose keeps its label.
        if before.endswith("(") and end < len(value) and value[end] == ")":
            result.append(before[:-1])
            end += 1
        elif start > cursor and value[start - 1] == "!":
            result.append(before[:-1])
        else:
            result.extend((before, label))
        cursor = end
    text = "".join(result)
    text = re.sub(r"[^]*", "", text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: m[1] or m[2], text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text, sources, rejected
