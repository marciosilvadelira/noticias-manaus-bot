import unittest

from noticias_bot.facebook import GraphClient, GraphError, classify_error, exchange_for_page_token
from noticias_bot.http_client import HttpClient, NetworkError

from .helpers import fake_transport

G = "https://graph.facebook.com/v26.0"


def client(routes):
    t = fake_transport(routes)
    return GraphClient("123", "TOKEN", http=HttpClient(transport=t, sleep=lambda s: None)), t


class ClassifyTests(unittest.TestCase):
    def kind(self, status, code=None, **extra):
        body = {"error": {"code": code, "message": "x", **extra}} if code is not None else {}
        return classify_error(status, body).kind

    def test_classificacao(self):
        self.assertEqual(self.kind(400, 190, error_subcode=463), "auth")
        self.assertEqual(self.kind(400, 200), "auth")
        self.assertEqual(self.kind(403, 10), "auth")
        self.assertEqual(self.kind(400, 4), "throttle")
        self.assertEqual(self.kind(400, 368), "blocked")
        self.assertEqual(self.kind(400, 506), "duplicate")
        self.assertEqual(self.kind(500, 2), "transient")
        self.assertEqual(self.kind(400, 100), "permanent")
        self.assertEqual(self.kind(502), "uncertain")
        self.assertTrue(classify_error(400, {"error": {"code": 190}}).stops_run)


class ClientTests(unittest.TestCase):
    def test_publica_link(self):
        c, t = client({f"{G}/123/feed": (200, {"id": "123_999"})})
        self.assertEqual(c.publish_link("msg", "https://marioadolfo.com/a/"), "123_999")
        method, url, data = t.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn(b"message=msg", data)
        self.assertIn(b"access_token=TOKEN", data)
        self.assertNotIn("TOKEN", url)  # token não vai na URL de publicação

    def test_publica_foto_com_caption(self):
        c, t = client({f"{G}/123/photos": (200, {"id": "555", "post_id": "123_777"})})
        self.assertEqual(c.publish_photo("https://img/x.jpg", "legenda"), "123_777")
        self.assertIn(b"caption=legenda", t.calls[0][2])

    def test_timeout_na_publicacao_e_incerto_e_nao_repete(self):
        c, t = client({f"{G}/123/feed": NetworkError("timeout", timed_out=True)})
        with self.assertRaises(GraphError) as ctx:
            c.publish_link("m", "l")
        self.assertEqual(ctx.exception.kind, "uncertain")
        self.assertEqual(len(t.calls), 1)

    def test_token_expirado(self):
        c, _ = client({f"{G}/me": (400, {"error": {"code": 190, "error_subcode": 463, "message": "expired"}})})
        with self.assertRaises(GraphError) as ctx:
            c.verify_page_token()
        self.assertEqual(ctx.exception.kind, "auth")

    def test_token_de_outra_pagina_e_recusado(self):
        c, _ = client({f"{G}/me": (200, {"id": "999", "name": "Outra Página"})})
        with self.assertRaises(GraphError) as ctx:
            c.verify_page_token()
        self.assertIn("Outra Página", str(ctx.exception))

    def test_token_correto(self):
        c, _ = client({f"{G}/me": (200, {"id": "123", "name": "Notícias Manaus"})})
        self.assertEqual(c.verify_page_token()["name"], "Notícias Manaus")

    def test_troca_de_token(self):
        t = fake_transport({
            f"{G}/oauth/access_token": (200, {"access_token": "LONGUSER"}),
            f"{G}/me/accounts": (200, {"data": [{"id": "123", "name": "Notícias Manaus", "access_token": "PAGE"}]}),
        })
        page = exchange_for_page_token("SHORT", "app", "sec", "123", http=HttpClient(transport=t))
        self.assertEqual(page["access_token"], "PAGE")


if __name__ == "__main__":
    unittest.main()
