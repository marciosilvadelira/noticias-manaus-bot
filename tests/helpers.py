"""Utilitários de teste: feeds sintéticos, fonte falsa e Graph API falsa (sem rede)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

from noticias_bot.facebook import GraphError
from noticias_bot.http_client import Response
from noticias_bot.sources.base import SourceError
from noticias_bot.sources.ghost_rss import GhostRSSAdapter

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)  # 11h em Manaus


def make_item(n: int, minutes_ago: int = 30, category: str = "Amazonas", body_words: int = 120) -> dict:
    body = " ".join(f"palavra{i}" for i in range(body_words))
    return {
        "title": f"Notícia de teste número {n}",
        "link": f"https://marioadolfo.com/noticia-teste-{n}/",
        "guid": f"guid{n:04d}",
        "category": category,
        "description": f"Linha fina da notícia {n}",
        "date": NOW - timedelta(minutes=minutes_ago),
        "image": f"https://s3.example/img{n}.jpg",
        "content": f"<p>{body} O caso ocorreu em 2026 com 35 pessoas.</p>",
    }


def make_feed(items: list[dict]) -> bytes:
    parts = []
    for it in items:
        parts.append(
            "<item>"
            f"<title><![CDATA[{it['title']}]]></title>"
            f"<description><![CDATA[{it.get('description', '')}]]></description>"
            f"<link>{it['link']}</link>"
            f"<guid isPermaLink=\"false\">{it['guid']}</guid>"
            f"<category><![CDATA[{it.get('category', '')}]]></category>"
            f"<pubDate>{format_datetime(it['date'], usegmt=True)}</pubDate>"
            + (f"<media:content url=\"{it['image']}\" medium=\"image\"/>" if it.get("image") else "")
            + f"<content:encoded><![CDATA[{it.get('content', '')}]]></content:encoded>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:media="http://search.yahoo.com/mrss/" version="2.0"><channel><title>Portal Mário Adolfo</title>'
        + "".join(parts) + "</channel></rss>"
    ).encode("utf-8")


class FakeSource:
    """Fonte controlável pelos testes."""
    name = "fake"

    def __init__(self, items: list[dict] | None = None):
        self.items = items or []
        self.fail: str | None = None
        self._parser = GhostRSSAdapter([])

    def fetch(self):
        if self.fail:
            raise SourceError(self.fail)
        return self._parser.parse(make_feed(self.items))


class FakeGraph:
    """Imita a Graph API: registra publicações e permite injetar falhas."""

    def __init__(self, page_id: str = "123"):
        self.page_id = page_id
        self.posts: list[dict] = []          # o que "existe" na Página
        self.publish_calls = 0
        self.next_errors: list[GraphError] = []
        self.ghost_publish_on_error = False  # simula: a Meta publicou, mas a resposta se perdeu
        self.token_error: GraphError | None = None
        self.feed_error: GraphError | None = None
        self.photo_error: GraphError | None = None

    def verify_page_token(self):
        if self.token_error:
            raise self.token_error
        return {"id": self.page_id, "name": "Notícias Manaus (teste)"}

    def recent_posts(self, limit=50):
        if self.feed_error:
            raise self.feed_error
        return list(reversed(self.posts))[:limit]

    def _store(self, message):
        pid = f"{self.page_id}_{len(self.posts) + 1}"
        self.posts.append({"id": pid, "message": message})
        return pid

    def publish_link(self, message, link):
        self.publish_calls += 1
        if self.next_errors:
            err = self.next_errors.pop(0)
            if self.ghost_publish_on_error:
                self._store(message)
            raise err
        return self._store(message)

    def publish_photo(self, url, caption):
        self.publish_calls += 1
        if self.photo_error:
            raise self.photo_error
        return self._store(caption)


def fake_transport(routes: dict):
    """Cria um transport HTTP que responde por prefixo de URL, sem rede.

    routes: {prefixo_url: (status, corpo_dict_ou_bytes) | Exception}
    """
    calls = []

    def transport(method, url, data, headers, timeout):
        calls.append((method, url, data))
        for prefix, result in routes.items():
            if url.startswith(prefix):
                if isinstance(result, Exception):
                    raise result
                status, body = result
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return Response(status, raw, {})
        return Response(404, b"{}", {})

    transport.calls = calls
    return transport
