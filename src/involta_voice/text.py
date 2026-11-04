"""Restore catalogue spelling without changing words or grammatical forms."""

import re


def restore_names(text: str, names: list[str]) -> str:
    canonical: dict[str, str] = {}
    for name in names:
        name = " ".join(name.split())
        if name:
            canonical.setdefault(name.lower(), name)
    if not canonical:
        return text
    names = sorted(canonical.values(), key=len, reverse=True)
    alternatives = [
        f"(?P<n{i}>" + r"\s+".join(re.escape(word) for word in name.split()) + ")"
        for i, name in enumerate(names)
    ]
    pattern = r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)"
    return re.sub(
        pattern,
        lambda match: names[int(str(match.lastgroup)[1:])],
        text,
        flags=re.IGNORECASE,
    )
