"""Resumo opcional por IA (Formato B), com verificações automáticas de fidelidade.

Usa a API da Anthropic (Claude) somente se ANTHROPIC_API_KEY estiver definida.
Sem chave, ou se o resumo gerado não passar nas verificações, o sistema publica
no Formato A (link) — nunca publica um resumo duvidoso.

Verificações aplicadas ao texto gerado:
  1. Tamanho entre ~55 e 110 palavras (pedido: 60 a 100).
  2. Todo número citado no resumo precisa existir no texto da matéria.
  3. Nenhum trecho de 10 palavras seguidas copiado da matéria.
  4. Sem aspas (para não haver declarações atribuídas que possam ser imprecisas).
"""

from __future__ import annotations

import logging
import re
import unicodedata

from .http_client import HttpClient, NetworkError
from .sources.base import Article

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MAX_SOURCE_CHARS = 8000
MIN_WORDS, MAX_WORDS = 55, 110
COPY_WINDOW = 10

SYSTEM_PROMPT = (
    "Você é um editor de jornalismo local em Manaus. Escreva em português do Brasil. "
    "Sua tarefa é escrever um resumo ORIGINAL, com suas próprias palavras, de uma matéria jornalística. "
    "Regras obrigatórias:\n"
    "- Entre 60 e 100 palavras, em um único parágrafo, linguagem jornalística clara e neutra.\n"
    "- Use SOMENTE informações presentes no texto fornecido. Não acrescente contexto, opiniões, "
    "números, datas, nomes ou detalhes que não estejam no texto.\n"
    "- Não use aspas e não reproduza declarações literais; se necessário, relate de forma indireta "
    "(ex.: 'segundo o governador...').\n"
    "- Não copie frases da matéria; reescreva.\n"
    "- Não inclua título, link, hashtags, emojis nem crédito — isso é adicionado depois.\n"
    "- Responda apenas com o parágrafo do resumo."
)


class SummaryRejected(Exception):
    pass


def _normalize_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text.lower())
    return re.findall(r"[\wÀ-ÿ]+", text)


def _numbers(text: str) -> set[str]:
    found = set()
    for raw in re.findall(r"\d[\d.,]*\d|\d", text):
        found.add(re.sub(r"[.,]", "", raw))
    return found


def validate_summary(summary: str, source_text: str) -> None:
    """Levanta SummaryRejected se o resumo violar alguma regra de fidelidade."""
    words = summary.split()
    if not (MIN_WORDS <= len(words) <= MAX_WORDS):
        raise SummaryRejected(f"tamanho fora do intervalo: {len(words)} palavras")

    if re.search(r"[\"“”«»]", summary):
        raise SummaryRejected("contém aspas")

    src_numbers = _numbers(source_text)
    invented = [n for n in _numbers(summary) if n not in src_numbers]
    if invented:
        raise SummaryRejected(f"números ausentes na matéria: {', '.join(sorted(invented))}")

    src_words = _normalize_words(source_text)
    windows = {tuple(src_words[i:i + COPY_WINDOW]) for i in range(max(0, len(src_words) - COPY_WINDOW + 1))}
    sw = _normalize_words(summary)
    for i in range(max(0, len(sw) - COPY_WINDOW + 1)):
        if tuple(sw[i:i + COPY_WINDOW]) in windows:
            raise SummaryRejected("trecho copiado literalmente da matéria")


class AnthropicSummarizer:
    """Gera resumos com a API da Anthropic. Custos são cobrados pela Anthropic por uso."""

    def __init__(self, api_key: str, model: str = "claude-haiku-5-5", http: HttpClient | None = None):
        self.api_key = api_key
        self.model = model
        self.http = http or HttpClient(timeout=60)

    def source_text(self, article: Article) -> str:
        body = article.plain_text()[:MAX_SOURCE_CHARS]
        return f"{article.title}\n{article.description}\n{body}"

    def summarize(self, article: Article) -> str | None:
        """Devolve o resumo validado, ou None (o chamador então usa o Formato A)."""
        source = self.source_text(article)
        if len(article.plain_text().split()) < 60:
            log.info("Matéria curta demais para resumo seguro: %s", article.url)
            return None
        payload = {
            "model": self.model,
            "max_tokens": 400,
            "system": SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": f"Título: {article.title}\nLinha fina: {article.description}\n\n"
                           f"Texto da matéria:\n{article.plain_text()[:MAX_SOURCE_CHARS]}",
            }],
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        try:
            resp = self.http.post_json(ANTHROPIC_URL, payload, headers)
        except NetworkError as exc:
            log.warning("IA indisponível (%s); usando formato link.", exc)
            return None
        if resp.status != 200:
            log.warning("IA retornou HTTP %s; usando formato link. Detalhe: %s",
                        resp.status, resp.text()[:300])
            return None
        try:
            data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        except (ValueError, AttributeError):
            log.warning("Resposta da IA ilegível; usando formato link.")
            return None
        summary = " ".join(text.split())
        try:
            validate_summary(summary, source)
        except SummaryRejected as exc:
            log.warning("Resumo rejeitado (%s) para %s; usando formato link.", exc, article.url)
            return None
        return summary
