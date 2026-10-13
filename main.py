"""Ponto de entrada do automatizador Notícias Manaus.

Comandos (use: python main.py <comando>):
  run             Executa um ciclo completo (padrão). Respeita DRY_RUN e PAUSED.
  check-source    Lê o RSS do portal e mostra as matérias por seção (não altera nada).
  preview         Mostra como as 3 matérias mais recentes seriam publicadas (não altera nada).
  check-token     Confere se o token da Meta é válido e pertence à Página configurada.
  status          Resume o histórico salvo.
  exchange-token  (uso local) Troca um token curto de usuário por um token de Página permanente.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone

from noticias_bot.config import Config, load_dotenv
from noticias_bot.facebook import GraphClient, GraphError, exchange_for_page_token
from noticias_bot.formatter import build_link_post, build_summary_post
from noticias_bot.publisher import build_publisher
from noticias_bot.sources import GhostRSSAdapter, SourceError
from noticias_bot.storage import Store
from noticias_bot.summarizer import AnthropicSummarizer


def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def write_step_summary(title: str, lines: list[str]) -> None:
    """No GitHub Actions, grava um resumo legível na página da execução."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"## {title}\n\n```\n" + "\n".join(lines) + "\n```\n")


def cmd_run(cfg: Config) -> int:
    report = build_publisher(cfg).run_once()
    write_step_summary(f"Execução ({report.mode})", report.lines)
    return report.exit_code


def cmd_check_source(cfg: Config) -> int:
    try:
        articles = GhostRSSAdapter(cfg.feed_urls).fetch()
    except SourceError as exc:
        print(f"ERRO: {exc}")
        return 1
    by_cat = Counter(a.category or "(sem seção)" for a in articles)
    lines = [f"Feeds: {', '.join(cfg.feed_urls)}", f"Matérias encontradas: {len(articles)}", "", "Por seção:"]
    lines += [f"  {cat}: {n}" for cat, n in by_cat.most_common()]
    lines += ["", "Mais recentes:"]
    for a in sorted(articles, key=lambda x: x.published_at, reverse=True)[:10]:
        when = a.published_at.astimezone().strftime("%d/%m %H:%M")
        img = "com imagem" if a.image_url else "sem imagem"
        lines.append(f"  {when} [{a.category}] {a.title} ({img})\n      {a.url}")
    print("\n".join(lines))
    write_step_summary("Verificação da fonte", lines)
    return 0


def cmd_preview(cfg: Config) -> int:
    try:
        articles = GhostRSSAdapter(cfg.feed_urls).fetch()
    except SourceError as exc:
        print(f"ERRO: {exc}")
        return 1
    summarizer = AnthropicSummarizer(cfg.anthropic_api_key, cfg.ai_model) if cfg.ai_enabled else None
    lines: list[str] = []
    for a in sorted(articles, key=lambda x: x.published_at, reverse=True)[:3]:
        lines += ["=" * 60, "FORMATO A (link):", build_link_post(a).message, ""]
        if summarizer:
            s = summarizer.summarize(a)
            lines += ["FORMATO B (resumo):",
                      build_summary_post(a, s).message if s else "(resumo rejeitado/indisponível — usaria o formato A)", ""]
        else:
            lines += ["FORMATO B (resumo): IA não configurada — usaria o formato A.", ""]
    print("\n".join(lines))
    write_step_summary("Prévia das publicações", lines)
    return 0


def cmd_check_token(cfg: Config) -> int:
    if not cfg.fb_page_id or not cfg.fb_page_access_token:
        print("ERRO: defina FB_PAGE_ID e FB_PAGE_ACCESS_TOKEN.")
        return 2
    graph = GraphClient(cfg.fb_page_id, cfg.fb_page_access_token, cfg.graph_api_version)
    lines = []
    try:
        me = graph.verify_page_token()
        lines.append(f"OK: o token pertence à Página '{me['name']}' (id {me['id']}).")
    except GraphError as exc:
        lines.append(f"ERRO: {exc}")
        print("\n".join(lines))
        write_step_summary("Verificação do token", lines)
        return 2
    try:
        posts = graph.recent_posts(limit=3)
        lines.append(f"OK: leitura das publicações da Página funcionando ({len(posts)} recentes lidas).")
    except GraphError as exc:
        if exc.kind not in {"auth", "permanent"}:
            lines.append(f"ERRO temporário ao ler a Página: {exc}. Tente de novo em alguns minutos.")
            print("\n".join(lines))
            write_step_summary("Verificação do token", lines)
            return 2
        lines.append(f"AVISO: a Meta não permite ler as publicações da Página com este app ({exc}).")
        lines.append("       A publicação pode funcionar mesmo assim; o robô usa o histórico para evitar "
                     "duplicatas. Faça a publicação de teste para confirmar.")
    if cfg.fb_app_id and cfg.fb_app_secret:
        try:
            info = graph.debug_token(cfg.fb_app_id, cfg.fb_app_secret)
            exp = info.get("expires_at", 0)
            exp_txt = "nunca (token de Página permanente)" if not exp else \
                datetime.fromtimestamp(exp, timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
            scopes = ", ".join(info.get("scopes", []))
            lines.append(f"Tipo: {info.get('type')} | válido: {info.get('is_valid')} | expira: {exp_txt}")
            lines.append(f"Permissões: {scopes}")
            missing = {"pages_manage_posts", "pages_read_engagement"} - set(info.get("scopes", []))
            if missing:
                lines.append(f"ATENÇÃO: faltam permissões: {', '.join(sorted(missing))}")
        except GraphError as exc:
            lines.append(f"(Não foi possível detalhar o token: {exc})")
    else:
        lines.append("Dica: defina FB_APP_ID e FB_APP_SECRET para ver data de expiração e permissões.")
    print("\n".join(lines))
    write_step_summary("Verificação do token", lines)
    return 0


def cmd_status(cfg: Config) -> int:
    store = Store(cfg.state_file)
    lines = [f"Arquivo: {cfg.state_file}",
             f"Referência inicial criada em: {store.meta.get('bootstrapped_at') or '(ainda não)'}",
             f"Última execução: {store.meta.get('last_run_at') or '-'}",
             f"Por status: {dict(sorted(store.stats().items()))}",
             f"Publicadas/simuladas hoje: {store.count_today(cfg.timezone, {'published', 'simulated'})}"]
    recent = sorted(store.items.values(), key=lambda r: r.get("updated_at") or "", reverse=True)[:10]
    lines.append("Últimos registros:")
    for r in recent:
        lines.append(f"  {r['status']:<10} {r.get('fb_post_id') or '-':<28} {r['title'][:70]}")
    print("\n".join(lines))
    write_step_summary("Status do histórico", lines)
    return 0


def cmd_exchange_token(cfg: Config) -> int:
    short = os.environ.get("FB_USER_TOKEN") or input("Cole o token de USUÁRIO (curto) do Graph API Explorer: ").strip()
    app_id = cfg.fb_app_id or input("ID do aplicativo: ").strip()
    secret = cfg.fb_app_secret or input("Chave secreta do aplicativo: ").strip()
    page_id = cfg.fb_page_id or input("ID da Página: ").strip()
    try:
        page = exchange_for_page_token(short, app_id, secret, page_id, cfg.graph_api_version)
    except GraphError as exc:
        print(f"ERRO: {exc}")
        return 2
    print(f"\nPágina: {page['name']} ({page['id']})  tarefas: {', '.join(page.get('tasks', []))}")
    print("Token de Página (guarde como segredo FB_PAGE_ACCESS_TOKEN; não compartilhe):\n")
    print(page["access_token"])
    return 0


COMMANDS = {
    "run": cmd_run,
    "check-source": cmd_check_source,
    "preview": cmd_preview,
    "check-token": cmd_check_token,
    "status": cmd_status,
    "exchange-token": cmd_exchange_token,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automatizador Notícias Manaus")
    parser.add_argument("command", nargs="?", default="run", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    load_dotenv()
    setup_logging()
    cfg = Config.from_env()
    return COMMANDS[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
