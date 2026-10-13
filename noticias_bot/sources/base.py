"""Contrato comum dos adaptadores de leitura de notícias.

Se a estrutura do portal mudar (por exemplo, se o RSS deixar de existir), basta
criar outro adaptador que implemente `fetch()` e devolva objetos `Article`.
O restante do sistema não precisa ser alterado.
"""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class SourceError(Exception):
    """A fonte está indisponível ou retornou dados ambíguos. Nada deve ser publicado."""


@dataclass
class Article:
    url: str                    # URL canônica
    title: str
    published_at: datetime | None
    guid: str = ""
    description: str = ""
    category: str = ""          # seção principal (1ª categoria do RSS)
    tags: list[str] = field(default_factory=list)
    author: str = ""
    image_url: str = ""
    content_html: str = ""      # texto integral, usado SOMENTE como base do resumo (nunca publicado)

    @property
    def key(self) -> str:
        """Identificador estável: GUID do Ghost quando existir; senão, hash da URL canônica."""
        if self.guid:
            return f"guid:{self.guid}"
        return "url:" + hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:24]

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:24]

    def plain_text(self) -> str:
        return html_to_text(self.content_html)


class SourceAdapter(Protocol):
    name: str

    def fetch(self) -> list[Article]:
        ...


def canonical_url(url: str) -> str:
    """Normaliza a URL: https, sem 'www.', sem parâmetros de rastreamento, com barra final."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    return urllib.parse.urlunsplit(("https", host, path, "", ""))


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</(p|h[1-6]|li|blockquote|div)>|<br\s*/?>", re.I)
_WS_RE = re.compile(r"[ \t ]+")


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    text = _BLOCK_RE.sub("\n", raw)
    text = re.sub(r"<(script|style|figure)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)
