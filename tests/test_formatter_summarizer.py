import unittest

from noticias_bot.formatter import build_link_post, build_summary_post, detect_format
from noticias_bot.http_client import HttpClient
from noticias_bot.sources.ghost_rss import GhostRSSAdapter
from noticias_bot.summarizer import AnthropicSummarizer, SummaryRejected, validate_summary

from .helpers import fake_transport, make_feed, make_item

SOURCE = ("O governo estadual reuniu prefeitos de 36 municípios nesta segunda-feira para coordenar o combate "
          "às queimadas. Segundo o comandante dos bombeiros, as áreas prioritárias são o entorno de Manaus e o "
          "Baixo Amazonas. Duas aeronaves já atuam na região e outras aguardam melhores condições de visibilidade.")

GOOD = ("Prefeitos de 36 municípios do Amazonas participaram de encontro com o governo estadual para alinhar "
        "ações contra os incêndios florestais. De acordo com o comando do Corpo de Bombeiros, o esforço se "
        "concentra nos arredores da capital e na região do Baixo Amazonas. Duas aeronaves já foram empregadas "
        "nas operações, enquanto os demais aviões dependem de melhora na visibilidade, prejudicada pela fumaça, "
        "para começar a atuar. A reunião também tratou de medidas para os próximos meses de estiagem no estado.")


def article(**kw):
    return GhostRSSAdapter([]).parse(make_feed([make_item(1, **kw)]))[0]


class FormatterTests(unittest.TestCase):
    def test_formato_link(self):
        p = build_link_post(article())
        self.assertEqual(p.format, "link")
        self.assertTrue(p.message.startswith("Notícia de teste número 1"))
        self.assertIn("Linha fina da notícia 1", p.message)
        self.assertIn("Fonte: Portal Mário Adolfo", p.message)
        self.assertIn("https://marioadolfo.com/noticia-teste-1/", p.message)
        self.assertEqual(p.link, "https://marioadolfo.com/noticia-teste-1/")
        self.assertEqual(p.image_url, "https://s3.example/img1.jpg")
        self.assertNotIn("palavra5", p.message)  # nunca copia o corpo da matéria
        self.assertEqual(detect_format(p.message), "link")

    def test_formato_resumo(self):
        p = build_summary_post(article(), GOOD)
        self.assertEqual(p.format, "resumo")
        self.assertIn("Resumo elaborado a partir de reportagem do Portal Mário Adolfo", p.message)
        self.assertIn("https://marioadolfo.com/noticia-teste-1/", p.message)
        self.assertEqual(detect_format(p.message), "resumo")


class ValidationTests(unittest.TestCase):
    def test_resumo_bom_passa(self):
        validate_summary(GOOD, SOURCE)

    def test_rejeita_numero_inventado(self):
        with self.assertRaises(SummaryRejected):
            validate_summary(GOOD.replace("Duas aeronaves", "Doze aeronaves e 48 brigadistas"), SOURCE)

    def test_rejeita_curto_ou_longo(self):
        with self.assertRaises(SummaryRejected):
            validate_summary("Texto curto demais.", SOURCE)
        with self.assertRaises(SummaryRejected):
            validate_summary(GOOD + " " + GOOD, SOURCE)

    def test_rejeita_copia_literal(self):
        copied = GOOD.rsplit(".", 2)[0] + ". " + "Segundo o comandante dos bombeiros, as áreas prioritárias são o entorno de Manaus."
        with self.assertRaises(SummaryRejected):
            validate_summary(copied, SOURCE)

    def test_rejeita_aspas(self):
        with self.assertRaises(SummaryRejected):
            validate_summary(GOOD.replace("De acordo", "“Vamos vencer”, disse. De acordo"), SOURCE)


class SummarizerTests(unittest.TestCase):
    def _summ(self, routes):
        t = fake_transport(routes)
        return AnthropicSummarizer("chave", http=HttpClient(transport=t, sleep=lambda s: None)), t

    def test_resposta_valida_da_ia(self):
        art = article()
        text = ("Uma notícia de teste do portal informa um caso ocorrido em 2026 que envolveu 35 pessoas. "
                "O texto original descreve a situação em detalhes, com diversas informações apresentadas ao "
                "longo da reportagem, e o resumo apenas reorganiza os dados principais com palavras próprias, "
                "sem acrescentar fatos, nomes ou números que não constem da matéria publicada pelo veículo. "
                "Os leitores podem conferir todos os pormenores no endereço indicado ao final da publicação.")
        s, t = self._summ({"https://api.anthropic.com": (200, {"content": [{"type": "text", "text": text}]})})
        self.assertEqual(s.summarize(art), text)
        self.assertIn(b'"model": "claude-haiku-5-5"', t.calls[0][2])

    def test_erro_da_ia_volta_para_link(self):
        s, _ = self._summ({"https://api.anthropic.com": (500, {"error": "x"})})
        self.assertIsNone(s.summarize(article()))

    def test_resumo_com_numero_inventado_e_descartado(self):
        bad = "Foram 999 casos. " + " ".join(["texto"] * 70)
        s, _ = self._summ({"https://api.anthropic.com": (200, {"content": [{"type": "text", "text": bad}]})})
        self.assertIsNone(s.summarize(article()))

    def test_materia_curta_nao_e_resumida(self):
        s, t = self._summ({})
        self.assertIsNone(s.summarize(article(body_words=10)))
        self.assertEqual(t.calls, [])


if __name__ == "__main__":
    unittest.main()
