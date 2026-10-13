import unittest

from noticias_bot.http_client import HttpClient, NetworkError
from noticias_bot.sources import GhostRSSAdapter, SourceError, canonical_url

from .helpers import FIXTURES, fake_transport

FEED = "https://marioadolfo.com/latest/rss/"


def adapter_with(routes):
    return GhostRSSAdapter([FEED], http=HttpClient(transport=fake_transport(routes), sleep=lambda s: None))


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.xml = (FIXTURES / "feed_amostra.xml").read_bytes()

    def test_le_campos_do_rss_real(self):
        arts = adapter_with({FEED: (200, self.xml)}).fetch()
        self.assertEqual(len(arts), 3)  # 2 itens inválidos descartados
        aviao = next(a for a in arts if "Aviões" in a.title)
        self.assertEqual(aviao.category, "Amazonas")
        self.assertEqual(aviao.guid, "6acd32be52c09500099dc2c5")
        self.assertEqual(aviao.key, "guid:6acd32be52c09500099dc2c5")
        self.assertTrue(aviao.image_url.endswith(".jpg"))
        self.assertIn("três voos", aviao.description)
        self.assertEqual(aviao.author, "Redação BMA")
        self.assertEqual(aviao.published_at.year, 2026)
        self.assertIn("número 12", aviao.plain_text())

    def test_url_canonica(self):
        arts = adapter_with({FEED: (200, self.xml)}).fetch()
        parintins = next(a for a in arts if a.category == "Parintins")
        self.assertEqual(parintins.url,
                         "https://marioadolfo.com/prefeitura-de-parintins-divulga-programacao-especial-pelos-174-anos-do-municipio/")
        self.assertEqual(parintins.image_url, "")
        self.assertEqual(canonical_url("http://www.marioadolfo.com/abc?x=1#y"), "https://marioadolfo.com/abc/")

    def test_ordena_do_mais_antigo_para_o_mais_novo(self):
        arts = adapter_with({FEED: (200, self.xml)}).fetch()
        dates = [a.published_at for a in arts]
        self.assertEqual(dates, sorted(dates))

    def test_fonte_fora_do_ar(self):
        with self.assertRaises(SourceError):
            adapter_with({FEED: NetworkError("timeout", timed_out=True)}).fetch()

    def test_bloqueio_nao_e_contornado(self):
        t = fake_transport({FEED: (403, b"forbidden")})
        a = GhostRSSAdapter([FEED], http=HttpClient(transport=t, sleep=lambda s: None))
        with self.assertRaises(SourceError) as ctx:
            a.fetch()
        self.assertIn("negado", str(ctx.exception))
        self.assertEqual(len(t.calls), 1)  # uma tentativa só, sem insistir

    def test_conteudo_invalido(self):
        with self.assertRaises(SourceError):
            adapter_with({FEED: (200, b"<html>manutencao</html>")}).fetch()
        with self.assertRaises(SourceError):
            adapter_with({FEED: (200, b"isto nao e xml")}).fetch()

    def test_estrutura_mudou_e_nenhum_item_valido(self):
        xml = b'<rss version="2.0"><channel><item><title>x</title></item></channel></rss>'
        with self.assertRaises(SourceError):
            adapter_with({FEED: (200, xml)}).fetch()

    def test_varios_feeds_sem_duplicar(self):
        tag = "https://marioadolfo.com/policia/rss/"
        a = GhostRSSAdapter([FEED, tag], http=HttpClient(
            transport=fake_transport({FEED: (200, self.xml), tag: (200, self.xml)}), sleep=lambda s: None))
        self.assertEqual(len(a.fetch()), 3)


if __name__ == "__main__":
    unittest.main()
