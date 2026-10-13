"""Integração com a Graph API oficial da Meta para publicar na Página.

Documentação consultada (out/2026):
- Publicar: POST /{page-id}/feed com 'message' e 'link'; resposta {"id": "..."}.
  https://developers.facebook.com/docs/pages-api/posts/
- Foto: POST /{page-id}/photos com 'url' e 'caption'; resposta {"id","post_id"}.
  https://developers.facebook.com/docs/graph-api/reference/page/photos
- Permissões: pages_manage_posts, pages_read_engagement (+ tarefa CREATE_CONTENT na Página).
- Token de Página de longa duração não tem data de expiração (pode ser invalidado
  por troca de senha, remoção do app, perda de função na Página etc.).
  https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived/
- Códigos de erro: https://developers.facebook.com/docs/graph-api/guides/error-handling
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .http_client import HttpClient, NetworkError

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"

AUTH_CODES = {10, 102, 190}            # token inválido/expirado ou permissão ausente
THROTTLE_CODES = {4, 17, 32, 341, 613}  # limite de chamadas: parar e tentar depois
TRANSIENT_CODES = {1, 2}                # instabilidade temporária
POLICY_BLOCK_CODES = {368}              # bloqueio temporário por política: parar
DUPLICATE_CODES = {506}                 # publicação idêntica recente: provavelmente já existe


@dataclass
class GraphError(Exception):
    kind: str           # auth | throttle | transient | blocked | duplicate | permanent | uncertain
    message: str
    code: int | None = None
    subcode: int | None = None
    http_status: int | None = None

    def __str__(self) -> str:
        bits = [self.kind]
        if self.code is not None:
            bits.append(f"código {self.code}" + (f"/{self.subcode}" if self.subcode else ""))
        if self.http_status:
            bits.append(f"HTTP {self.http_status}")
        return f"[{', '.join(bits)}] {self.message}"

    @property
    def stops_run(self) -> bool:
        """Erros que tornam inútil (ou arriscado) continuar publicando nesta execução."""
        return self.kind in {"auth", "throttle", "blocked"}


def classify_error(status: int, body: dict | None) -> GraphError:
    err = (body or {}).get("error") or {}
    code = err.get("code")
    sub = err.get("error_subcode")
    msg = err.get("message") or f"HTTP {status}"
    if code in AUTH_CODES or (isinstance(code, int) and 200 <= code <= 299):
        return GraphError("auth", msg, code, sub, status)
    if code in THROTTLE_CODES:
        return GraphError("throttle", msg, code, sub, status)
    if code in POLICY_BLOCK_CODES:
        return GraphError("blocked", msg, code, sub, status)
    if code in DUPLICATE_CODES:
        return GraphError("duplicate", msg, code, sub, status)
    if code in TRANSIENT_CODES or err.get("is_transient"):
        return GraphError("transient", msg, code, sub, status)
    if status >= 500 or not err:
        # Sem corpo de erro reconhecível ou erro do servidor: não sabemos se publicou.
        return GraphError("uncertain", msg, code, sub, status)
    return GraphError("permanent", msg, code, sub, status)


class GraphClient:
    def __init__(self, page_id: str, page_token: str, version: str = "v26.0",
                 http: HttpClient | None = None):
        self.page_id = page_id
        self.token = page_token
        self.version = version
        self.http = http or HttpClient(timeout=30)

    def _url(self, path: str) -> str:
        return f"{GRAPH}/{self.version}/{path.lstrip('/')}"

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["access_token"] = self.token
        try:
            resp = self.http.get(self._url(path), params, retries=2)
        except NetworkError as exc:
            raise GraphError("transient", f"falha de rede: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status != 200 or "error" in data:
            raise classify_error(resp.status, data)
        return data

    def _post(self, path: str, fields: dict) -> dict:
        fields = dict(fields)
        fields["access_token"] = self.token
        try:
            resp = self.http.post_form(self._url(path), fields)
        except NetworkError as exc:
            # A requisição pode ter chegado à Meta; tratamos como incerto.
            raise GraphError("uncertain", f"sem resposta da Meta: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status != 200 or "error" in data:
            raise classify_error(resp.status, data)
        return data

    # ---------- validação ----------
    def whoami(self) -> dict:
        """Com um token de PÁGINA, /me devolve a própria Página (id e nome)."""
        return self._get("me", {"fields": "id,name"})

    def verify_page_token(self) -> dict:
        """Garante que o token pertence exatamente à Página configurada."""
        me = self.whoami()
        if str(me.get("id")) != str(self.page_id):
            raise GraphError(
                "auth",
                f"O token pertence a '{me.get('name')}' (id {me.get('id')}), e não à Página "
                f"configurada (FB_PAGE_ID={self.page_id}). Gere um token da Página correta.",
            )
        return me

    def debug_token(self, app_id: str, app_secret: str) -> dict:
        """Mostra validade, expiração e permissões do token (requer ID e chave secreta do app)."""
        try:
            resp = self.http.get(self._url("debug_token"), {
                "input_token": self.token,
                "access_token": f"{app_id}|{app_secret}",
            })
        except NetworkError as exc:
            raise GraphError("transient", f"falha de rede: {exc}") from exc
        data = resp.json() if resp.body else {}
        if resp.status != 200 or "error" in data:
            raise classify_error(resp.status, data)
        return data.get("data", {})

    # ---------- leitura ----------
    def recent_posts(self, limit: int = 50) -> list[dict]:
        data = self._get(f"{self.page_id}/feed", {
            "fields": "id,message,created_time,permalink_url",
            "limit": str(limit),
        })
        return data.get("data", [])

    # ---------- publicação ----------
    def publish_link(self, message: str, link: str) -> str:
        data = self._post(f"{self.page_id}/feed", {"message": message, "link": link})
        post_id = data.get("id")
        if not post_id:
            raise GraphError("uncertain", f"resposta sem id: {data}")
        return post_id

    def publish_photo(self, image_url: str, caption: str) -> str:
        data = self._post(f"{self.page_id}/photos", {"url": image_url, "caption": caption})
        post_id = data.get("post_id") or data.get("id")
        if not post_id:
            raise GraphError("uncertain", f"resposta sem id: {data}")
        return post_id


def exchange_for_page_token(short_user_token: str, app_id: str, app_secret: str, page_id: str,
                            version: str = "v26.0", http: HttpClient | None = None) -> dict:
    """Troca um token de usuário (curto) por um token de Página de longa duração.

    1) GET /oauth/access_token?grant_type=fb_exchange_token -> token de usuário (~60 dias)
    2) GET /me/accounts com esse token -> token da Página (sem data de expiração)
    """
    http = http or HttpClient()
    base = f"{GRAPH}/{version}"
    r1 = http.get(f"{base}/oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_user_token,
    })
    d1 = r1.json()
    if r1.status != 200 or "access_token" not in d1:
        raise classify_error(r1.status, d1)
    long_user = d1["access_token"]
    r2 = http.get(f"{base}/me/accounts", {"access_token": long_user, "fields": "id,name,access_token,tasks"})
    d2 = r2.json()
    if r2.status != 200 or "data" not in d2:
        raise classify_error(r2.status, d2)
    for page in d2["data"]:
        if str(page.get("id")) == str(page_id):
            return page
    names = ", ".join(f"{p.get('name')} ({p.get('id')})" for p in d2["data"]) or "nenhuma"
    raise GraphError("auth", f"Página {page_id} não encontrada entre as que você administra: {names}")
