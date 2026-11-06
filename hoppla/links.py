"""Links that a command on a host prints (e.g. Jupyter's "http://localhost:8888/lab?token=...")."""

from __future__ import annotations

import re

LOCAL_URL = re.compile(r"""(https?)://(localhost|127\.0\.0\.1)(?::(\d+))?(/[^\s"'<>]*)?""")


def local_links(line: str) -> list[tuple[str, int, str]]:
    """The localhost links in `line` as (scheme, port, path-and-query)."""
    out = []
    for m in LOCAL_URL.finditer(line):
        scheme, _, port, rest = m.groups()
        port = int(port) if port else (443 if scheme == "https" else 80)
        out.append((scheme, port, (rest or "").rstrip(".,;)]")))
    return out


def preferred_link(old: str | None, new: str) -> str:
    """Which of two links to use. Servers often print their address first without and then with a login
    token: for the same server (scheme, host, port) a link with a token beats one without, and a longer link
    that starts with the other beats it; a link without a token never replaces one with a token. A link to
    another server is newer news and replaces the old one."""
    if old is None:
        return new

    def origin(u: str) -> str:
        scheme, _, rest = u.partition("://")
        return scheme + "://" + rest.split("/", 1)[0].split("?", 1)[0]

    if origin(old) != origin(new):
        return new
    old_token, new_token = "token=" in old, "token=" in new
    if new_token and not old_token:
        return new
    if old_token and not new_token:
        return old
    if len(new) > len(old) and new.startswith(old.rstrip("/")):
        return new
    return old
