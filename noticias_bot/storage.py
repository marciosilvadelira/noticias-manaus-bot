"""Histórico persistente das notícias processadas (arquivo JSON).

O arquivo fica no próprio repositório (data/historico.json) e é salvo de volta
pelo GitHub Actions ao fim de cada execução. Assim o histórico sobrevive a
reinicializações e nenhuma notícia é processada duas vezes.

Status possíveis de cada notícia:
  baseline   -> já existia na primeira execução; nunca será publicada
  skipped    -> ignorada (categoria fora do filtro, antiga demais etc.)
  pending    -> nova, aguardando vaga no limite de publicações
  simulated  -> processada em modo de simulação (não foi ao Facebook)
  publishing -> publicação em andamento (se o processo cair aqui, a próxima
                execução confere no Facebook antes de tentar de novo)
  uncertain  -> a chamada à Meta não teve resposta clara; será conferida
  retry      -> erro temporário; nova tentativa na próxima execução
  published  -> publicada com sucesso (fb_post_id registrado)
  failed     -> erro definitivo ou tentativas esgotadas
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .sources.base import Article

FINAL_STATUSES = {"baseline", "skipped", "simulated", "published", "failed"}
OPEN_STATUSES = {"pending", "publishing", "uncertain", "retry"}
SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = {"schema": SCHEMA_VERSION, "meta": {}, "items": {}}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self.data["meta"] = loaded.get("meta", {})
            self.data["items"] = loaded.get("items", {})

    # ---------- meta ----------
    @property
    def meta(self) -> dict:
        return self.data["meta"]

    @property
    def bootstrapped(self) -> bool:
        return bool(self.meta.get("bootstrapped_at"))

    def mark_bootstrapped(self) -> None:
        self.meta["bootstrapped_at"] = iso(utcnow())

    # ---------- itens ----------
    @property
    def items(self) -> dict:
        return self.data["items"]

    def get(self, key: str) -> dict | None:
        return self.items.get(key)

    def has_url(self, url: str) -> bool:
        return any(r.get("url") == url for r in self.items.values())

    def contains(self, article: Article) -> bool:
        return article.key in self.items or self.has_url(article.url)

    def add(self, article: Article, status: str, fmt: str = "", note: str = "") -> dict:
        record = {
            "key": article.key,
            "url": article.url,
            "url_hash": article.url_hash,
            "guid": article.guid,
            "title": article.title,
            "category": article.category,
            "source_published_at": iso(article.published_at),
            "discovered_at": iso(utcnow()),
            "status": status,
            "formats": [fmt] if fmt else [],
            "posts": {},            # formato -> id da publicação no Facebook
            "fb_post_id": None,     # id da primeira publicação (atalho)
            "attempts": [],         # histórico de tentativas
            "note": note,
            "updated_at": iso(utcnow()),
        }
        self.items[article.key] = record
        return record

    def set_status(self, record: dict, status: str, note: str | None = None) -> None:
        record["status"] = status
        if note is not None:
            record["note"] = note
        record["updated_at"] = iso(utcnow())

    def log_attempt(self, record: dict, fmt: str, result: str, fb_post_id: str | None = None,
                    error: str | None = None) -> None:
        record["attempts"].append({
            "at": iso(utcnow()),
            "format": fmt,
            "result": result,
            "fb_post_id": fb_post_id,
            "error": error,
        })
        if fb_post_id:
            record["posts"][fmt] = fb_post_id
            record["fb_post_id"] = record["fb_post_id"] or fb_post_id
        record["updated_at"] = iso(utcnow())

    def open_records(self) -> list[dict]:
        recs = [r for r in self.items.values() if r.get("status") in OPEN_STATUSES]
        recs.sort(key=lambda r: r.get("source_published_at") or "")
        return recs

    def count_today(self, tz_name: str, statuses: set[str], now: datetime | None = None) -> int:
        """Quantas publicações (reais ou simuladas) foram feitas 'hoje' no fuso configurado."""
        tz = ZoneInfo(tz_name)
        today = (now or utcnow()).astimezone(tz).date()
        total = 0
        for rec in self.items.values():
            for att in rec.get("attempts", []):
                if att.get("result") not in statuses:
                    continue
                at = parse_iso(att.get("at"))
                if at and at.astimezone(tz).date() == today:
                    total += 1
        return total

    def prune(self, retention_days: int) -> int:
        """Remove registros antigos e já finalizados para o arquivo não crescer sem limite.

        Isso é seguro porque a fonte só lista as matérias mais recentes e notícias
        com mais de MAX_AGE_HOURS nunca são publicadas.
        """
        limit = utcnow() - timedelta(days=retention_days)
        removed = 0
        for key in list(self.items):
            rec = self.items[key]
            seen = parse_iso(rec.get("updated_at")) or parse_iso(rec.get("discovered_at"))
            if rec.get("status") in FINAL_STATUSES and seen and seen < limit:
                del self.items[key]
                removed += 1
        return removed

    def stats(self) -> dict:
        out: dict[str, int] = {}
        for rec in self.items.values():
            out[rec.get("status", "?")] = out.get(rec.get("status", "?"), 0) + 1
        return out

    # ---------- disco ----------
    def save(self) -> None:
        """Gravação atômica: escreve num arquivo temporário e depois substitui."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.meta["saved_at"] = iso(utcnow())
        fd, tmp = tempfile.mkstemp(prefix=".historico-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
