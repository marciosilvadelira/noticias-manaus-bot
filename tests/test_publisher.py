import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from noticias_bot.config import Config
from noticias_bot.facebook import GraphError
from noticias_bot.publisher import Publisher
from noticias_bot.storage import Store

from .helpers import NOW, FakeGraph, FakeSource, make_item


class FakeSummarizer:
    def __init__(self, text="Resumo de teste aprovado."):
        self.text = text

    def summarize(self, article):
        return self.text


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "historico.json"
        self.source = FakeSource([make_item(i, minutes_ago=300 - i) for i in range(1, 6)])
        self.graph = FakeGraph()
        self.now = NOW

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **kw):
        base = dict(dry_run=False, fb_page_id="123", fb_page_access_token="T",
                    post_interval_seconds=0, state_file=str(self.state))
        base.update(kw)
        return Config(**base)

    def run_bot(self, cfg=None, summarizer=None):
        cfg = cfg or self.cfg()
        store = Store(self.state)  # recarrega do disco: simula um processo novo a cada execução
        pub = Publisher(cfg, store, self.source, self.graph, summarizer,
                        sleep=lambda s: None, now=lambda: self.now)
        return pub.run_once(), store

    def bootstrap(self, cfg=None):
        return self.run_bot(cfg)

    # ---------------- primeira execução ----------------
    def test_primeira_execucao_nao_publica_antigas(self):
        rep, store = self.bootstrap()
        self.assertEqual(self.graph.publish_calls, 0)
        self.assertEqual(store.stats(), {"baseline": 5})
        self.assertTrue(store.bootstrapped)

    # ---------------- detecção e publicação ----------------
    def test_nova_noticia_e_publicada_uma_unica_vez(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        rep, store = self.run_bot()
        self.assertEqual(rep.published, 1)
        self.assertEqual(self.graph.publish_calls, 1)
        rec = store.get("guid:guid0006")
        self.assertEqual(rec["status"], "published")
        self.assertEqual(rec["fb_post_id"], "123_1")
        self.assertEqual(rec["attempts"][0]["result"], "published")
        # Execuções seguintes (novos processos) não repetem
        for _ in range(3):
            self.run_bot()
        self.assertEqual(self.graph.publish_calls, 1)

    def test_mudanca_de_url_com_parametros_nao_duplica(self):
        self.bootstrap()
        it = make_item(6, minutes_ago=5)
        self.source.items.append(it)
        self.run_bot()
        it["link"] = "http://www.marioadolfo.com/noticia-teste-6?utm=fb"
        it["guid"] = "guid-mudou"
        self.run_bot()
        self.assertEqual(self.graph.publish_calls, 1)

    def test_simulacao_nao_chama_meta_e_nao_publica_depois(self):
        self.bootstrap(self.cfg(dry_run=True))
        self.source.items.append(make_item(6, minutes_ago=5))
        rep, store = self.run_bot(self.cfg(dry_run=True))
        self.assertEqual(rep.simulated, 1)
        self.assertEqual(self.graph.publish_calls, 0)
        self.assertEqual(store.get("guid:guid0006")["status"], "simulated")
        # Ao ligar o modo real, a notícia já simulada NÃO é publicada (evita enxurrada)
        self.run_bot(self.cfg(dry_run=False))
        self.assertEqual(self.graph.publish_calls, 0)

    def test_limite_por_execucao_e_fila(self):
        self.bootstrap()
        self.source.items += [make_item(i, minutes_ago=60 - i) for i in range(10, 18)]  # 8 novas
        rep, store = self.run_bot()
        self.assertEqual(rep.published, 5)
        rep2, store = self.run_bot()
        self.assertEqual(rep2.published, 3)
        self.assertEqual(self.graph.publish_calls, 8)

    def test_limite_diario(self):
        self.bootstrap()
        self.source.items += [make_item(i, minutes_ago=200 - i) for i in range(10, 35)]  # 25 novas
        total = 0
        for _ in range(6):
            rep, store = self.run_bot()
            total += rep.published
        self.assertEqual(total, 20)
        self.assertEqual(store.count_today("America/Manaus", {"published"}, NOW), 20)
        # No dia seguinte (em Manaus), o restante elegível volta a ser publicado
        self.now = NOW + timedelta(hours=14)
        self.source.items.append(make_item(99, minutes_ago=-14 * 60 + 5))
        rep, store = self.run_bot()
        self.assertGreaterEqual(rep.published, 1)

    def test_ordem_cronologica(self):
        self.bootstrap()
        self.source.items += [make_item(21, minutes_ago=10), make_item(20, minutes_ago=20)]
        self.run_bot()
        self.assertIn("número 20", self.graph.posts[0]["message"])
        self.assertIn("número 21", self.graph.posts[1]["message"])

    def test_filtro_de_categorias(self):
        self.bootstrap()
        self.source.items += [make_item(30, 5, "Polícia"), make_item(31, 5, "Parintins")]
        rep, store = self.run_bot(self.cfg(categories=["parintins"]))
        self.assertEqual(rep.published, 1)
        self.assertEqual(store.get("guid:guid0030")["status"], "skipped")
        self.assertEqual(store.get("guid:guid0031")["status"], "published")

    def test_noticia_antiga_nao_publicada_mesmo_se_historico_sumir(self):
        self.bootstrap()
        self.source.items.append(make_item(40, minutes_ago=60 * 30))  # 30h atrás
        rep, store = self.run_bot()
        self.assertEqual(self.graph.publish_calls, 0)
        self.assertEqual(store.get("guid:guid0040")["status"], "skipped")

    # ---------------- falhas ----------------
    def test_fonte_fora_do_ar_nao_publica_e_alerta_depois_de_1h(self):
        self.bootstrap()
        self.source.fail = "fora do ar"
        codes = [self.run_bot()[0].exit_code for _ in range(6)]
        self.assertEqual(codes, [0, 0, 0, 0, 0, 1])
        self.assertEqual(self.graph.publish_calls, 0)
        self.source.fail = None
        rep, store = self.run_bot()
        self.assertEqual(store.meta["consecutive_source_failures"], 0)

    def test_fonte_fora_do_ar_na_primeira_execucao_nao_cria_referencia(self):
        self.source.fail = "fora do ar"
        rep, store = self.run_bot()
        self.assertFalse(store.bootstrapped)

    def test_resultado_incerto_e_conferido_antes_de_repetir_publicada(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        self.graph.next_errors = [GraphError("uncertain", "timeout")]
        self.graph.ghost_publish_on_error = True  # a Meta publicou, mas não respondeu
        rep, store = self.run_bot()
        self.assertEqual(store.get("guid:guid0006")["status"], "uncertain")
        self.assertEqual(len(self.graph.posts), 1)
        rep, store = self.run_bot()
        rec = store.get("guid:guid0006")
        self.assertEqual(rec["status"], "published")
        self.assertEqual(rec["fb_post_id"], "123_1")
        self.assertEqual(len(self.graph.posts), 1)  # sem duplicata
        self.assertEqual(self.graph.publish_calls, 1)

    def test_resultado_incerto_nao_publicado_e_tentado_de_novo(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        self.graph.next_errors = [GraphError("uncertain", "timeout")]
        self.run_bot()
        rep, store = self.run_bot()
        self.assertEqual(store.get("guid:guid0006")["status"], "published")
        self.assertEqual(len(self.graph.posts), 1)
        self.assertEqual(self.graph.publish_calls, 2)

    def test_queda_no_meio_da_execucao_sem_historico_salvo(self):
        """Publicou, mas o histórico não chegou a ser gravado (ex.: o servidor caiu)."""
        self.bootstrap()
        snapshot = self.state.read_text(encoding="utf-8")
        self.source.items.append(make_item(6, minutes_ago=5))
        self.run_bot()
        self.state.write_text(snapshot, encoding="utf-8")  # perde o registro da publicação
        rep, store = self.run_bot()
        self.assertEqual(self.graph.publish_calls, 1)
        self.assertEqual(store.get("guid:guid0006")["status"], "published")

    def test_erro_temporario_tenta_ate_o_limite(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        self.graph.next_errors = [GraphError("transient", "x")] * 3
        statuses = []
        for _ in range(3):
            rep, store = self.run_bot()
            statuses.append(store.get("guid:guid0006")["status"])
        self.assertEqual(statuses, ["retry", "retry", "failed"])
        self.run_bot()
        self.assertEqual(self.graph.publish_calls, 3)

    def test_token_invalido_bloqueia_publicacao(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        self.graph.token_error = GraphError("auth", "expirado", 190)
        rep, store = self.run_bot()
        self.assertEqual(rep.exit_code, 2)
        self.assertEqual(self.graph.publish_calls, 0)
        self.graph.token_error = None
        rep, store = self.run_bot()
        self.assertEqual(rep.published, 1)

    def test_sem_credenciais_modo_real_nao_publica(self):
        self.bootstrap(self.cfg(dry_run=True))
        rep, _ = self.run_bot(self.cfg(fb_page_access_token=""))
        self.assertEqual(rep.exit_code, 2)

    def test_erro_de_permissao_interrompe_execucao(self):
        self.bootstrap()
        self.source.items += [make_item(6, 6), make_item(7, 5)]
        self.graph.next_errors = [GraphError("auth", "sem permissão", 200)]
        rep, store = self.run_bot()
        self.assertEqual(rep.exit_code, 2)
        self.assertEqual(self.graph.publish_calls, 1)
        self.assertEqual(store.get("guid:guid0007")["status"], "pending")

    def test_falha_ao_conferir_pagina_nao_publica(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        self.graph.feed_error = GraphError("transient", "x")
        rep, store = self.run_bot()
        self.assertEqual(self.graph.publish_calls, 0)
        self.assertEqual(store.get("guid:guid0006")["status"], "pending")

    def test_pausa(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        rep, store = self.run_bot(self.cfg(paused=True))
        self.assertEqual(self.graph.publish_calls, 0)
        self.assertIsNone(store.get("guid:guid0006"))

    # ---------------- formatos ----------------
    def test_formato_resumo_sem_ia_vira_link(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        rep, store = self.run_bot(self.cfg(post_format="resumo"))
        self.assertIn("Fonte: Portal Mário Adolfo", self.graph.posts[0]["message"])
        self.assertEqual(store.get("guid:guid0006")["formats"], ["link"])

    def test_formato_resumo_com_ia(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        rep, store = self.run_bot(self.cfg(post_format="resumo", anthropic_api_key="k"), FakeSummarizer())
        msg = self.graph.posts[0]["message"]
        self.assertIn("Resumo de teste aprovado.", msg)
        self.assertIn("Resumo elaborado a partir de reportagem do Portal Mário Adolfo", msg)
        self.assertEqual(len(self.graph.posts), 1)

    def test_resumo_rejeitado_vira_link(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        self.run_bot(self.cfg(post_format="resumo", anthropic_api_key="k"), FakeSummarizer(text=None))
        self.assertIn("Fonte: Portal Mário Adolfo", self.graph.posts[0]["message"])
        self.assertEqual(len(self.graph.posts), 1)

    def test_dois_formatos_somente_quando_habilitado(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        rep, store = self.run_bot(self.cfg(post_format="resumo", anthropic_api_key="k",
                                           allow_both_formats=True), FakeSummarizer())
        self.assertEqual(len(self.graph.posts), 2)
        rec = store.get("guid:guid0006")
        self.assertEqual(set(rec["posts"]), {"link", "resumo"})
        self.assertEqual(rec["status"], "published")

    def test_modo_foto_com_recuo_para_link(self):
        self.bootstrap()
        self.source.items.append(make_item(6, minutes_ago=5))
        self.graph.photo_error = GraphError("permanent", "formato de imagem não suportado", 100)
        rep, store = self.run_bot(self.cfg(image_mode="photo"))
        self.assertEqual(store.get("guid:guid0006")["status"], "published")
        self.assertEqual(len(self.graph.posts), 1)


if __name__ == "__main__":
    unittest.main()
