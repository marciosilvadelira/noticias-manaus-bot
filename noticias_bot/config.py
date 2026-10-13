"""Leitura da configuração a partir de variáveis de ambiente (ou de um arquivo .env).

Todas as opções têm valores padrão seguros: o modo de simulação (DRY_RUN) fica
LIGADO a menos que você escreva explicitamente DRY_RUN=false.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FEEDS = ["https://marioadolfo.com/latest/rss/"]
SOURCE_DOMAIN = "marioadolfo.com"
SOURCE_NAME = "Portal Mário Adolfo"


def load_dotenv(path: str | Path = ".env") -> None:
    """Carrega um arquivo .env simples (CHAVE=valor) sem sobrescrever o ambiente."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "sim", "yes", "on", "verdadeiro"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _list(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Config:
    # Segurança / controle
    dry_run: bool = True
    paused: bool = False

    # Fonte
    feed_urls: list[str] = field(default_factory=lambda: list(DEFAULT_FEEDS))
    categories: list[str] = field(default_factory=list)  # vazio = todas
    max_age_hours: int = 24

    # Limites
    max_posts_per_run: int = 5
    max_posts_per_day: int = 20
    max_attempts: int = 3
    post_interval_seconds: int = 20
    timezone: str = "America/Manaus"

    # Formato
    post_format: str = "link"  # "link" ou "resumo"
    allow_both_formats: bool = False
    image_mode: str = "preview"  # "preview" (cartão do link) ou "photo"

    # Facebook / Meta
    fb_page_id: str = ""
    fb_page_access_token: str = ""
    fb_app_id: str = ""
    fb_app_secret: str = ""
    graph_api_version: str = "v26.0"

    # IA (opcional)
    anthropic_api_key: str = ""
    ai_model: str = "claude-haiku-5-5"

    # Armazenamento
    state_file: str = "data/historico.json"
    history_retention_days: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        fmt = (e("POST_FORMAT") or "link").strip().lower()
        if fmt not in {"link", "resumo"}:
            fmt = "link"
        image_mode = (e("IMAGE_MODE") or "preview").strip().lower()
        if image_mode not in {"preview", "photo"}:
            image_mode = "preview"
        return cls(
            dry_run=_bool(e("DRY_RUN"), True),
            paused=_bool(e("PAUSED"), False),
            feed_urls=_list(e("FEED_URLS")) or list(DEFAULT_FEEDS),
            categories=_list(e("CATEGORIES")),
            max_age_hours=_int(e("MAX_AGE_HOURS"), 24),
            max_posts_per_run=_int(e("MAX_POSTS_PER_RUN"), 5),
            max_posts_per_day=_int(e("MAX_POSTS_PER_DAY"), 20),
            max_attempts=_int(e("MAX_ATTEMPTS"), 3),
            post_interval_seconds=_int(e("POST_INTERVAL_SECONDS"), 20),
            timezone=e("TIMEZONE") or "America/Manaus",
            post_format=fmt,
            allow_both_formats=_bool(e("ALLOW_BOTH_FORMATS"), False),
            image_mode=image_mode,
            fb_page_id=(e("FB_PAGE_ID") or "").strip(),
            fb_page_access_token=(e("FB_PAGE_ACCESS_TOKEN") or "").strip(),
            fb_app_id=(e("FB_APP_ID") or "").strip(),
            fb_app_secret=(e("FB_APP_SECRET") or "").strip(),
            graph_api_version=(e("GRAPH_API_VERSION") or "v26.0").strip(),
            anthropic_api_key=(e("ANTHROPIC_API_KEY") or "").strip(),
            ai_model=(e("AI_MODEL") or "claude-haiku-5-5").strip(),
            state_file=e("STATE_FILE") or "data/historico.json",
            history_retention_days=_int(e("HISTORY_RETENTION_DAYS"), 60),
        )

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    def formats_to_publish(self) -> list[str]:
        """Formatos que cada notícia deve receber, conforme a configuração."""
        if self.allow_both_formats:
            return ["link", "resumo"] if self.ai_enabled else ["link"]
        if self.post_format == "resumo" and not self.ai_enabled:
            return ["link"]  # sem IA, o resumo cai para o formato com link
        return [self.post_format]

    def describe(self) -> str:
        """Resumo da configuração sem expor segredos (seguro para logs públicos)."""
        return (
            f"modo={'SIMULACAO' if self.dry_run else 'PUBLICACAO REAL'} "
            f"pausado={self.paused} formato={self.post_format} "
            f"ambos_formatos={self.allow_both_formats} imagem={self.image_mode} "
            f"ia={'ligada' if self.ai_enabled else 'desligada'} "
            f"categorias={self.categories or 'todas'} "
            f"limites={self.max_posts_per_run}/execucao,{self.max_posts_per_day}/dia "
            f"pagina={'definida' if self.fb_page_id else 'NAO definida'} "
            f"token={'definido' if self.fb_page_access_token else 'NAO definido'}"
        )
