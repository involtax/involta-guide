"""Conservative nginx editor. Never evaluates nginx configuration as code."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

BEGIN = "# BEGIN INVOLTA GUIDE (managed)"
END = "# END INVOLTA GUIDE (managed)"
TOKEN = re.compile(
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\#[^\n]*|\$\{[^}]*\}|[{};]|[^\s{};\#]+"""
)


@dataclass
class Server:
    file: str
    text: str
    opening: int
    closing: int


def tokens(text):
    return [m for m in TOKEN.finditer(text) if not m.group().startswith("#")]


def servers(file, text):
    items = tokens(text)
    for i, token in enumerate(items[:-1]):
        if token.group() != "server" or items[i + 1].group() != "{":
            continue
        depth = 1
        for end in items[i + 2 :]:
            depth += (end.group() == "{") - (end.group() == "}")
            if depth == 0:
                yield Server(file, text, items[i + 1].end(), end.start())
                break
        else:
            raise ValueError("Unbalanced nginx server block")


def direct_directives(server):
    depth, current = 0, []
    for token in tokens(server.text[server.opening : server.closing]):
        value = token.group()
        if value == "{":
            depth += 1
            current = []
        elif value == "}":
            depth -= 1
            current = []
        elif depth == 0 and value == ";":
            yield current
            current = []
        elif depth == 0:
            current.append(value.strip("\"'"))


def select_server(dump, domain="involta.ru"):
    sections = re.split(r"(?m)^# configuration file ([^\n]+):\n", dump)
    matches = []
    for i in range(1, len(sections), 2):
        file, text = sections[i], sections[i + 1]
        if not PurePosixPath(file).is_absolute() or ".." in PurePosixPath(file).parts:
            raise ValueError("nginx -T returned a non-absolute configuration path")
        for server in servers(file, text):
            directives = list(direct_directives(server))
            named = any(d and d[0] == "server_name" and domain in d[1:] for d in directives)
            tls = any(
                d
                and d[0] == "listen"
                and "ssl" in d
                and any(v == "443" or v.endswith(":443") for v in d[1:])
                for d in directives
            )
            if named and tls:
                matches.append(server)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one explicit HTTPS server for {domain}; found {len(matches)}"
        )
    selected = matches[0]
    # Refuse existing routes anywhere in the expanded config, including included snippets.
    unmanaged = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), "", dump, flags=re.S)
    if re.search(r"\blocation\s+(?:(?:=|\^~|~\*?)\s+)?[^;{]*?/guide", unmanaged):
        raise ValueError("An unmanaged /guide location already exists; refusing to overwrite it")
    return selected


def render(text, server, upstream):
    if not re.fullmatch(r"(?:127\.0\.0\.1:[0-9]+|involta-guide:3000)", upstream):
        raise ValueError("Unsafe nginx upstream")
    block = f"""
    {BEGIN}
    location = /guide {{
        access_log off;
        return 302 /guide/$is_args$args;
    }}
    location ^~ /guide/ {{
        proxy_pass http://{upstream};
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_http_version 1.1;
        proxy_read_timeout 240s;
        client_max_body_size 11m;
        access_log off;
    }}
    {END}
"""
    body = text[server.opening : server.closing]
    if BEGIN in body or END in body:
        raise ValueError("Guide is already managed here; use deploy-production, not bootstrap")
    return text[: server.closing] + block + text[server.closing :]
