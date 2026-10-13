"""Orquestra uma execução completa: ler fonte -> detectar novidades -> publicar -> salvar.

Cada execução do agendador (a cada 10 minutos) chama `run_once` uma vez.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import Config
from .facebook import GraphClient, GraphError
from .formatter import PostPayload, build_link_post, build_summary_post, detect_format
from .sources.base import Article, SourceAdapter, SourceError, canonical_url
from .storage import Store, iso, parse_iso
from .summarizer import AnthropicSummarizer

log = logging.getLogger(__name__)

SOURCE_FAILURE_ALERT_THRESHOLD = 6  # ~1 hora de falhas seguidas -> execução marcada como erro


@dataclass
class RunReport:
    exit_code: int = 0
    mode: str = ""
    found: int = 0
    new: int = 0
    published: int = 0
    simulated: int = 0
    skipped: int = 0
    errors: int = 0
    lines: list[str] = field(default_factory=list)

    def add(self, line: str, level: int = logging.INFO) -> None:
        log.log(level, line)
        self.lines.append(line)


def _article_from_record(rec: dict) -> Article:
    return Article(
        url=rec["url"], title=rec.get("title", ""),
        published_at=parse_iso(rec.get("source_published_at")),
        guid=rec.get("guid", ""), description=rec.get("description", ""),
        category=rec.get("category", ""), image_url=rec.get("image_url", ""),
    )


def _url_in_text(url: str, text: str) -> bool:
    if not text:
        return False
    bare = url.replace("https://", "").rstrip("/")
    return bare in text


class Publisher:
    def __init__(self, config: Config, store: Store, source: SourceAdapter,
                 graph: GraphClient | None = None,
                 summarizer: AnthropicSummarizer | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] | None = None):
        self.cfg = config
        self.store = store
        self.source = source
        self.graph = graph
        self.summarizer = summarizer
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._articles: dict[str, Article] = {}

    # ------------------------------------------------------------------
    def run_once(self) -> RunReport:
        rep = RunReport(mode="simulação" if self.cfg.dry_run else "publicação real")
        rep.add(f"Início da execução — {self.cfg.describe()}")
        self.store.meta["last_run_at"] = iso(self.now())

        if self.cfg.paused:
            rep.add("Automação PAUSADA (PAUSED=true). Nada foi lido nem publicado.")
            self._save()
            return rep

        # 1) Pré-condições do modo real: o token precisa estar válido e ser da Página certa.
        if not self.cfg.dry_run:
            code = self._check_live_preconditions(rep)
            if code:
                rep.exit_code = code
                self._save()
                return rep

        # 2) Leitura da fonte
        try:
            articles = self.source.fetch()
        except SourceError as exc:
            fails = int(self.store.meta.get("consecutive_source_failures", 0)) + 1
            self.store.meta["consecutive_source_failures"] = fails
            self.store.meta["last_source_error"] = str(exc)
            rep.add(f"ERRO na fonte ({fails}ª falha seguida): {exc}. Nada será publicado.", logging.ERROR)
            if fails >= SOURCE_FAILURE_ALERT_THRESHOLD:
                rep.exit_code = 1
            self._save()
            return rep
        self.store.meta["consecutive_source_failures"] = 0
        rep.found = len(articles)
        self._articles = {a.key: a for a in articles}
        rep.add(f"Fonte lida: {len(articles)} matérias no feed.")

        # 3) Primeira execução: registrar tudo como referência, sem publicar.
        if not self.store.bootstrapped:
            for art in articles:
                self._add(art, "baseline", note="existente na primeira execução")
            self.store.mark_bootstrapped()
            rep.add(f"Primeira execução: {len(articles)} matérias registradas como referência. "
                    "Nenhuma foi publicada. A partir de agora, só notícias novas serão consideradas.")
            self._save()
            return rep

        # 4) Detectar novidades
        self._detect_new(articles, rep)

        # 5) Publicar (ou simular)
        self._process_queue(rep)

        removed = self.store.prune(self.cfg.history_retention_days)
        if removed:
            rep.add(f"Histórico: {removed} registros antigos removidos.")
        self._save()
        rep.add(f"Fim: novas={rep.new} publicadas={rep.published} simuladas={rep.simulated} "
                f"ignoradas={rep.skipped} erros={rep.errors}")
        return rep

    # ------------------------------------------------------------------
    def _save(self) -> None:
        self.store.save()

    def _add(self, art: Article, status: str, note: str = "") -> dict:
        rec = self.store.add(art, status, note=note)
        rec["description"] = art.description[:400]
        rec["image_url"] = art.image_url
        return rec

    def _check_live_preconditions(self, rep: RunReport) -> int:
        if not self.cfg.fb_page_id or not self.cfg.fb_page_access_token or self.graph is None:
            rep.add("Modo real solicitado, mas FB_PAGE_ID ou FB_PAGE_ACCESS_TOKEN não estão definidos. "
                    "Nada será publicado.", logging.ERROR)
            return 2
        try:
            me = self.graph.verify_page_token()
        except GraphError as exc:
            rep.add(f"Token da Meta inválido ou sem acesso: {exc}. Nada será publicado. "
                    "Veja o README, seção 'Renovar o token'.", logging.ERROR)
            return 2 if exc.kind in {"auth", "blocked"} else 0
        rep.add(f"Token validado para a Página '{me.get('name')}' ({me.get('id')}).")
        return 0

    def _detect_new(self, articles: list[Article], rep: RunReport) -> None:
        now = self.now()
        cutoff = now - timedelta(hours=self.cfg.max_age_hours)
        wanted = {c.lower() for c in self.cfg.categories}
        formats = self.cfg.formats_to_publish()
        for art in articles:
            if self.store.contains(art):
                continue
            if wanted and art.category.lower() not in wanted:
                self._add(art, "skipped", note=f"categoria '{art.category}' fora do filtro")
                rep.skipped += 1
                continue
            if art.published_at and art.published_at < cutoff:
                self._add(art, "skipped", note=f"publicada há mais de {self.cfg.max_age_hours}h")
                rep.skipped += 1
                continue
            rec = self._add(art, "pending")
            rec["formats"] = list(formats)
            rep.new += 1
            rep.add(f"Nova: [{art.category or 'sem seção'}] {art.title}")

    # ------------------------------------------------------------------
    def _process_queue(self, rep: RunReport) -> None:
        queue = self.store.open_records()
        if not queue:
            return

        # Descartar o que envelheceu na fila (ex.: limite diário atingido ontem).
        cutoff = self.now() - timedelta(hours=self.cfg.max_age_hours)
        for rec in list(queue):
            src = parse_iso(rec.get("source_published_at"))
            if rec["status"] in {"pending", "retry"} and src and src < cutoff and not rec["posts"]:
                self.store.set_status(rec, "skipped", "expirou na fila (mais antiga que o limite)")
                rep.skipped += 1
                queue.remove(rec)

        # Conferência no Facebook antes de qualquer publicação (evita duplicatas após
        # quedas, reinícios ou respostas incertas).
        if not self.cfg.dry_run and queue:
            if not self._reconcile(queue, rep):
                return

        published_today = self.store.count_today(self.cfg.timezone, {"published", "simulated"}, self.now())
        day_left = max(0, self.cfg.max_posts_per_day - published_today)
        run_left = self.cfg.max_posts_per_run
        first_post = True

        for rec in queue:
            missing = [f for f in rec.get("formats") or ["link"] if f not in rec["posts"]]
            if not missing:
                self.store.set_status(rec, "published")
                continue
            for fmt in missing:
                if run_left <= 0 or day_left <= 0:
                    why = "limite por execução" if run_left <= 0 else "limite diário"
                    rep.add(f"{why.capitalize()} atingido; restante fica na fila para a próxima execução.")
                    return
                if not first_post and not self.cfg.dry_run and self.cfg.post_interval_seconds > 0:
                    self.sleep(self.cfg.post_interval_seconds)
                first_post = False
                outcome = self._publish_one(rec, fmt, rep)
                if outcome in {"published", "simulated"}:
                    run_left -= 1
                    day_left -= 1
                if outcome == "stop":
                    return
            self._finalize(rec)

    def _finalize(self, rec: dict) -> None:
        if rec["status"] in {"uncertain", "retry", "failed"}:
            return
        formats = rec.get("formats") or ["link"]
        if all(f in rec["posts"] for f in formats):
            self.store.set_status(rec, "published")
        elif self.cfg.dry_run and any(a["result"] == "simulated" for a in rec["attempts"]):
            self.store.set_status(rec, "simulated")

    def _reconcile(self, queue: list[dict], rep: RunReport) -> bool:
        try:
            posts = self.graph.recent_posts(limit=50)
        except GraphError as exc:
            if exc.kind in {"auth", "permanent"}:
                # O token já foi validado no início da execução; aqui a Meta recusou
                # apenas a LEITURA do feed. Seguimos no modo seguro: o histórico local
                # evita duplicatas, e notícias com resultado incerto NÃO são repetidas.
                return self._reconcile_without_feed(queue, rep, exc)
            rep.add(f"Não foi possível conferir publicações recentes da Página ({exc}). "
                    "Por segurança, nada será publicado nesta execução.", logging.WARNING)
            if exc.kind == "blocked":
                rep.exit_code = 2
            return False
        for rec in queue:
            for post in posts:
                if _url_in_text(rec["url"], post.get("message", "")):
                    fmt = detect_format(post.get("message", ""))
                    if fmt not in rec["posts"]:
                        self.store.log_attempt(rec, fmt, "published", post.get("id"),
                                               error="confirmado na Página (recuperado)")
                        rep.add(f"Já estava publicada (confirmado no Facebook): {rec['title']}")
            if rec["status"] in {"publishing", "uncertain"}:
                missing = [f for f in rec.get("formats") or ["link"] if f not in rec["posts"]]
                if missing:
                    self.store.set_status(rec, "retry", "não encontrada na Página; nova tentativa")
                else:
                    self.store.set_status(rec, "published")
        return True

    def _reconcile_without_feed(self, queue: list[dict], rep: RunReport, exc: GraphError) -> bool:
        if not self.store.meta.get("feed_read_unavailable"):
            rep.add(f"Aviso: a Meta não permite que este app leia as publicações da Página ({exc}). "
                    "O robô segue publicando com base no histórico; notícias com resultado incerto "
                    "não serão repetidas automaticamente e ficam para conferência manual.",
                    logging.WARNING)
        self.store.meta["feed_read_unavailable"] = True
        for rec in queue:
            if rec["status"] in {"publishing", "uncertain"}:
                self.store.set_status(
                    rec, "failed",
                    "resultado incerto e sem como conferir na Página: verifique manualmente "
                    "se a publicação existe (não será repetida para evitar duplicata)")
                rep.add(f"CONFERIR NA PÁGINA (não será repetida): {rec['title']} — {rec['url']}",
                        logging.WARNING)
        queue[:] = [r for r in queue if r["status"] != "failed"]
        return True

    def _build_payload(self, rec: dict, fmt: str) -> PostPayload | None:
        art = self._articles.get(rec["key"]) or _article_from_record(rec)
        if fmt == "resumo":
            summary = None
            if self.summarizer and art.content_html:
                summary = self.summarizer.summarize(art)
            if summary:
                return build_summary_post(art, summary)
            if self.cfg.allow_both_formats:
                rec["formats"] = [f for f in rec["formats"] if f != "resumo"]
                return None  # o formato link já cobre esta notícia
            rec["formats"] = ["link" if f == "resumo" else f for f in rec["formats"]]
            fmt = "link"
        return build_link_post(art)

    def _publish_one(self, rec: dict, fmt: str, rep: RunReport) -> str:
        payload = self._build_payload(rec, fmt)
        if payload is None:
            return "skipped"
        fmt = payload.format

        if self.cfg.dry_run:
            self.store.log_attempt(rec, fmt, "simulated")
            self.store.set_status(rec, "simulated")
            rep.simulated += 1
            rep.add(f"[SIMULAÇÃO] Formato {fmt} — seria publicado:\n"
                    + "\n".join("    " + ln for ln in payload.message.splitlines()))
            return "simulated"

        self.store.set_status(rec, "publishing")
        self._save()  # registra a intenção antes de chamar a Meta
        try:
            if self.cfg.image_mode == "photo" and payload.image_url:
                try:
                    post_id = self.graph.publish_photo(payload.image_url, payload.message)
                except GraphError as exc:
                    if exc.kind != "permanent":
                        raise
                    rep.add(f"Imagem recusada pela Meta ({exc}); publicando como link.", logging.WARNING)
                    post_id = self.graph.publish_link(payload.message, payload.link)
            else:
                post_id = self.graph.publish_link(payload.message, payload.link)
        except GraphError as exc:
            return self._handle_error(rec, fmt, exc, rep)

        self.store.log_attempt(rec, fmt, "published", post_id)
        self.store.set_status(rec, "pending" if len(rec["posts"]) < len(rec["formats"]) else "published")
        rep.published += 1
        rep.add(f"Publicado ({fmt}) id={post_id}: {rec['title']}")
        return "published"

    def _handle_error(self, rec: dict, fmt: str, exc: GraphError, rep: RunReport) -> str:
        rep.errors += 1
        attempts = sum(1 for a in rec["attempts"] if a["result"] in {"error", "uncertain"})
        if exc.kind in {"uncertain", "duplicate"}:
            self.store.log_attempt(rec, fmt, "uncertain", error=str(exc))
            status = "failed" if attempts + 1 >= self.cfg.max_attempts else "uncertain"
            self.store.set_status(rec, status, "resultado incerto; será conferido antes de nova tentativa")
            rep.add(f"Resultado INCERTO ({exc}) — será conferido na Página na próxima execução: {rec['title']}",
                    logging.WARNING)
            return "error"
        self.store.log_attempt(rec, fmt, "error", error=str(exc))
        if exc.stops_run:
            self.store.set_status(rec, "retry", str(exc))
            if exc.kind in {"auth", "blocked"}:
                rep.exit_code = 2
            rep.add(f"Publicação interrompida: {exc}", logging.ERROR)
            return "stop"
        if exc.kind == "transient" and attempts + 1 < self.cfg.max_attempts:
            self.store.set_status(rec, "retry", str(exc))
            rep.add(f"Erro temporário ({exc}); nova tentativa depois: {rec['title']}", logging.WARNING)
        else:
            self.store.set_status(rec, "failed", str(exc))
            rep.add(f"Falha definitiva ({exc}): {rec['title']}", logging.ERROR)
        return "error"


def build_publisher(cfg: Config, store: Store | None = None, source: SourceAdapter | None = None) -> Publisher:
    from .sources.ghost_rss import GhostRSSAdapter
    store = store or Store(cfg.state_file)
    source = source or GhostRSSAdapter(cfg.feed_urls)
    graph = None
    if cfg.fb_page_id and cfg.fb_page_access_token:
        graph = GraphClient(cfg.fb_page_id, cfg.fb_page_access_token, cfg.graph_api_version)
    summarizer = AnthropicSummarizer(cfg.anthropic_api_key, cfg.ai_model) if cfg.ai_enabled else None
    return Publisher(cfg, store, source, graph, summarizer)


__all__ = ["Publisher", "RunReport", "build_publisher", "canonical_url"]
