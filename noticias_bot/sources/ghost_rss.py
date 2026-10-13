"""Adaptador para o RSS oficial do portal (plataforma Ghost).

Investigação feita em 13/10/2026:
- O portal usa Ghost 6.44 (meta generator), não WordPress.
- RSS oficial anunciado no rodapé: https://marioadolfo.com/latest/rss/
- Cada seção também tem RSS próprio, ex.: https://marioadolfo.com/policia/rss/
- Cada item traz: title, link, guid (ID interno do Ghost), pubDate, category
  (a 1ª é a seção: Amazonas, Polícia, Parintins, Brasil...), dc:creator,
  description (linha fina), media:content (imagem destacada) e content:encoded.
- robots.txt permite acesso geral ("Allow: /").
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from ..config import SOURCE_DOMAIN
from ..http_client import HttpClient, NetworkError
from .base import Article, SourceError, canonical_url

log = logging.getLogger(__name__)

NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}


class GhostRSSAdapter:
    name = "ghost-rss"

    def __init__(self, feed_urls: list[str], http: HttpClient | None = None,
                 allowed_domain: str = SOURCE_DOMAIN):
        self.feed_urls = feed_urls
        self.http = http or HttpClient()
        self.allowed_domain = allowed_domain

    def fetch(self) -> list[Article]:
        """Lê todos os feeds configurados. Se QUALQUER feed falhar, levanta SourceError.

        Falhar por inteiro é proposital: com dados parciais o sistema poderia
        tomar decisões erradas. Na próxima execução ele tenta de novo.
        """
        seen: dict[str, Article] = {}
        for url in self.feed_urls:
            for art in self._fetch_one(url):
                seen.setdefault(art.key, art)
        articles = list(seen.values())
        articles.sort(key=lambda a: (a.published_at is None, a.published_at), reverse=False)
        return articles

    def _fetch_one(self, feed_url: str) -> list[Article]:
        try:
            resp = self.http.get(feed_url)
        except NetworkError as exc:
            raise SourceError(f"Fonte indisponível ({feed_url}): {exc}") from exc
        if resp.status in (401, 403):
            # Não tentamos contornar bloqueios: apenas registramos e paramos.
            raise SourceError(f"Acesso negado pela fonte ({resp.status}) em {feed_url}. Nada será publicado.")
        if resp.status != 200:
            raise SourceError(f"Fonte respondeu HTTP {resp.status} em {feed_url}.")
        return self.parse(resp.body, feed_url)

    def parse(self, xml_bytes: bytes, feed_url: str = "") -> list[Article]:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise SourceError(f"RSS inválido em {feed_url}: {exc}") from exc
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise SourceError(f"O conteúdo de {feed_url} não é um RSS 2.0 reconhecível.")

        articles: list[Article] = []
        for item in channel.findall("item"):
            art = self._parse_item(item)
            if art is not None:
                articles.append(art)

        items_total = len(channel.findall("item"))
        if items_total and not articles:
            raise SourceError(f"Nenhum item válido em {feed_url} (estrutura pode ter mudado).")
        return articles

    def _parse_item(self, item: ET.Element) -> Article | None:
        title = (item.findtext("title") or "").strip()
        link = canonical_url(item.findtext("link") or "")
        guid = (item.findtext("guid") or "").strip()
        if not title or not link:
            log.warning("Item ignorado: sem título ou link (guid=%s)", guid or "?")
            return None
        host = link.split("/")[2]
        if host != self.allowed_domain and not host.endswith("." + self.allowed_domain):
            log.warning("Item ignorado: domínio inesperado %s", host)
            return None

        published = None
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date.strip())
            except (TypeError, ValueError):
                log.warning("Data inválida no item %s: %r", link, raw_date)
        if published is None:
            log.warning("Item ignorado: sem data verificável (%s)", link)
            return None

        cats = [(c.text or "").strip() for c in item.findall("category") if (c.text or "").strip()]
        media = item.find("media:content", NS)
        image = media.get("url", "") if media is not None else ""
        if not image:
            enc = item.find("enclosure")
            if enc is not None and (enc.get("type") or "").startswith("image/"):
                image = enc.get("url", "")

        return Article(
            url=link,
            title=title,
            published_at=published,
            guid=guid,
            description=(item.findtext("description") or "").strip(),
            category=cats[0] if cats else "",
            tags=cats[1:],
            author=(item.findtext("dc:creator", namespaces=NS) or "").strip(),
            image_url=image.strip(),
            content_html=item.findtext("content:encoded", namespaces=NS) or "",
        )
