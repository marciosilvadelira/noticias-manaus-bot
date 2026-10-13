"""Montagem do texto das publicações (Formato A: link; Formato B: resumo).

Cada publicação inclui a URL da matéria no próprio texto. Isso serve a dois
propósitos: o leitor sempre tem o link, e o sistema consegue conferir no
Facebook se uma publicação "incerta" de fato aconteceu (evitando duplicatas).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import SOURCE_NAME
from .sources.base import Article

SUMMARY_MARKER = "Resumo elaborado a partir de reportagem do"
MAX_DESCRIPTION_CHARS = 280


@dataclass
class PostPayload:
    format: str
    message: str
    link: str
    image_url: str = ""


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(",;:-–") + "…"


def build_link_post(article: Article) -> PostPayload:
    """Formato A — título original, linha fina (se houver), crédito e link."""
    parts = [article.title.strip()]
    desc = _clip(article.description, MAX_DESCRIPTION_CHARS)
    if desc and desc.lower() != article.title.strip().lower():
        parts.append(desc)
    parts.append(f"Fonte: {SOURCE_NAME}\nLeia a matéria completa: {article.url}")
    return PostPayload("link", "\n\n".join(parts), article.url, article.image_url)


def build_summary_post(article: Article, summary: str) -> PostPayload:
    """Formato B — título original, resumo próprio (60–100 palavras), crédito explícito e link."""
    message = "\n\n".join([
        article.title.strip(),
        " ".join(summary.split()),
        f"{SUMMARY_MARKER} {SOURCE_NAME}.\nLeia a matéria completa: {article.url}",
    ])
    return PostPayload("resumo", message, article.url, article.image_url)


def detect_format(message: str) -> str:
    return "resumo" if SUMMARY_MARKER in (message or "") else "link"
