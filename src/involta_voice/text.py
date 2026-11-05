"""Catalogue spelling and frozen, dictionary-backed inflections; never rewrite words."""

import re
from functools import cache
from typing import Any

CASES = ("nomn", "gent", "datv", "accs", "ablt", "loct", "loc2", "gen2")


@cache
def morphology() -> Any:
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


def word_parse(word: str) -> Any:
    # Unknown brands and mixed-case trademarks retain only their catalogue spelling.
    if not (word.islower() or word.isupper() or word.istitle()):
        return None
    parses = [
        p
        for p in morphology().parse(word)
        if p.is_known and "nomn" in p.tag and p.tag.POS in ("NOUN", "ADJF")
    ]
    return min(
        parses, key=lambda p: ("Geox" not in p.tag, p.tag.POS != "NOUN", -p.score), default=None
    )


def inflected_word(word: str, parsed: Any, case: str, *, animate: bool = False) -> str:
    if parsed is None or "Fixd" in parsed.tag:
        return word
    grammemes = {case}
    if parsed.tag.POS == "ADJF" and case == "accs":
        grammemes.add("anim" if animate else "inan")
    form = parsed.inflect(grammemes)
    if form is None:
        return word
    result = form.word
    if "ё" not in word.lower():
        result = result.replace("ё", "е")
    if word.isupper():
        return result.upper()
    if word[0].isupper():
        return result[0].upper() + result[1:]
    return result


def name_forms(name: str, *, city: bool = False) -> set[str]:
    words = list(re.finditer(r"[^\W\d_]+", name))
    parsed = [word_parse(m[0]) for m in words]
    forms = {name}
    # Restaurant names can retain an uninflected brand after their head noun:
    # Усадьба Плёс -> Усадьбе Плёс. City phrases agree as a whole.
    heads = [i for i, p in enumerate(parsed) if p is not None and p.tag.POS == "NOUN"]
    animate = not city and any("anim" in parsed[i].tag for i in heads)
    for case in CASES if len(words) == 1 else CASES[:6]:
        for head_only in (False, True) if not city and heads else (False,):
            pieces = []
            end = 0
            for i, (match, p) in enumerate(zip(words, parsed, strict=True)):
                pieces.append(name[end : match.start()])
                pieces.append(
                    inflected_word(match[0], p, case, animate=animate)
                    if not head_only or i == heads[0]
                    else match[0]
                )
                end = match.end()
            forms.add("".join(pieces) + name[end:])
    return forms


def catalogue_forms(names: list[str], *, city: str = "") -> dict[str, str]:
    """Build once at intake and freeze with the run, without guessing unknown words."""
    canonical = list(dict.fromkeys(" ".join(n.split()) for n in names if n.strip()))
    forms: dict[str, str] = {}
    ambiguous: set[str] = set()
    for name in canonical:
        for form in sorted(name_forms(name, city=name == city)):
            key = form.lower().replace("ё", "е")
            if key in ambiguous:
                continue
            if key in forms and forms[key] != form:
                ambiguous.add(key)
                forms.pop(key)
            else:
                forms[key] = form
    # Exact catalogue entries have priority over generated, possibly ambiguous forms.
    forms.update({name.lower().replace("ё", "е"): name for name in reversed(canonical)})
    return forms


def restore_names(text: str, names: list[str], *, forms: dict[str, str] | None = None) -> str:
    # Old runs have no frozen forms and keep their original exact-match behaviour.
    variants = forms
    if variants is None:
        variants = {}
        for name in names:
            name = " ".join(name.split())
            if name:
                variants.setdefault(name.lower(), name)
    if not variants:
        return text
    keys = sorted(variants, key=len, reverse=True)
    alternatives = []
    for i, name in enumerate(keys):
        escaped = r"\s+".join(re.escape(word) for word in name.split())
        if forms is not None:
            escaped = escaped.replace("е", "[её]")
        alternatives.append(f"(?P<n{i}>" + escaped + ")")
    pattern = r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)"
    return re.sub(
        pattern,
        lambda match: variants[keys[int(str(match.lastgroup)[1:])]],
        text,
        flags=re.IGNORECASE,
    )
