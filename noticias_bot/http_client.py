"""Cliente HTTP mínimo (somente biblioteca padrão) com tempo limite e retentativas.

O 'transport' pode ser substituído nos testes para simular respostas sem rede.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

USER_AGENT = "NoticiasManausBot/1.0 (+leitor de RSS; contato via Pagina Noticias Manaus)"


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class NetworkError(Exception):
    """Falha de rede: sem resposta do servidor (DNS, conexão recusada, tempo esgotado)."""

    def __init__(self, message: str, timed_out: bool = False):
        super().__init__(message)
        self.timed_out = timed_out


Transport = Callable[[str, str, Optional[bytes], dict, float], Response]


def urllib_transport(method: str, url: str, data: Optional[bytes], headers: dict, timeout: float) -> Response:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers))
    except urllib.error.HTTPError as exc:  # resposta HTTP com erro (4xx/5xx)
        return Response(exc.code, exc.read() or b"", dict(exc.headers or {}))
    except (socket.timeout, TimeoutError) as exc:
        raise NetworkError(f"tempo esgotado: {exc}", timed_out=True) from exc
    except urllib.error.URLError as exc:
        timed_out = isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError))
        raise NetworkError(f"erro de rede: {exc.reason}", timed_out=timed_out) from exc


class HttpClient:
    def __init__(self, transport: Transport | None = None, timeout: float = 20.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.transport = transport or urllib_transport
        self.timeout = timeout
        self.sleep = sleep

    def get(self, url: str, params: dict | None = None, retries: int = 2) -> Response:
        """GET com retentativas para falhas de rede e erros 5xx/429 (operação segura)."""
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self.transport("GET", url, None, headers, self.timeout)
                if resp.status in (429, 500, 502, 503, 504) and attempt < retries:
                    self.sleep(2 ** attempt * 2)
                    continue
                return resp
            except NetworkError as exc:
                last_exc = exc
                if attempt < retries:
                    self.sleep(2 ** attempt * 2)
        assert last_exc is not None
        raise last_exc

    def post_form(self, url: str, fields: dict) -> Response:
        """POST sem retentativa automática: publicar duas vezes seria uma duplicação.

        Quem chama decide o que fazer com falhas incertas.
        """
        data = urllib.parse.urlencode(fields).encode("utf-8")
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
        return self.transport("POST", url, data, headers, self.timeout)

    def post_json(self, url: str, payload: dict, headers: dict | None = None) -> Response:
        data = json.dumps(payload).encode("utf-8")
        h = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        h.update(headers or {})
        return self.transport("POST", url, data, h, max(self.timeout, 60.0))
