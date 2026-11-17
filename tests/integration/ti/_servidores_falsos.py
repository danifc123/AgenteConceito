"""Servidores HTTP falsos (de verdade, em socket TCP, cada um no seu thread
com loop próprio) pro GLPI e pra OCI — usados por `test_cenarios_poller.py`
pra rodar o poller de triagem com os clientes REAIS (`ClienteGLPIReal`,
`AsyncOpenAI`, SDK nativo da OCI com assinatura RSA) contra cenários de
produção: provedor lento, travado, devolvendo erro, GLPI fora do ar etc.

Socket de verdade importa: timeout de `httpx`/`requests` só é aplicado pelo
transporte de rede, um mock em memória nunca deixaria o cliente "esperando".

Só implementa as rotas que o poller usa, com o mesmo formato de JSON que o
cliente real espera (ver `tools/ti/glpi.py`)."""

import asyncio
import hashlib
import json
import math
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_TRAVADO_SEGUNDOS = 3600


class ServidorFalso:
    """Sobe um app Starlette num thread, porta livre aleatória."""

    def __init__(self, app: Starlette):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self.porta = self._socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.porta}"
        config = uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=0)
        self._servidor = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._rodar, daemon=True)

    def _rodar(self) -> None:
        asyncio.run(self._servidor.serve(sockets=[self._socket]))

    def iniciar(self) -> "ServidorFalso":
        self._thread.start()
        limite = time.monotonic() + 5
        while not self._servidor.started:
            if time.monotonic() > limite:
                raise RuntimeError("servidor falso não subiu")
            time.sleep(0.01)
        return self

    def parar(self) -> None:
        self._servidor.should_exit = True
        self._servidor.force_exit = True
        self._thread.join(timeout=5)


# ---------------------------------------------------------------- GLPI ---


@dataclass
class EstadoGLPI:
    tickets: dict[int, dict] = field(default_factory=dict)
    followups: dict[int, list[dict]] = field(default_factory=dict)
    documentos_por_ticket: dict[int, list[int]] = field(default_factory=dict)
    documentos: dict[int, tuple[str, str, bytes]] = field(default_factory=dict)  # id -> (nome, mime, bytes)
    pending_reasons: set[int] = field(default_factory=set)
    conta_ia_id: int = 999
    conta_ia_login: str = "conta.ia"
    # Comportamentos de falha
    travar: bool = False  # toda rota fica sem responder
    atraso_segundos: float = 0.0
    requisicoes: int = 0

    def adicionar_ticket(
        self,
        ticket_id: int,
        titulo: str,
        descricao: str,
        categoria: tuple[int, str] | None,
        status: int = 1,
        solicitante: str = "joao.silva",
    ) -> None:
        self.tickets[ticket_id] = {
            "id": ticket_id,
            "name": titulo,
            "content": descricao,
            "status": {"id": status, "name": ""},
            "category": {"id": categoria[0], "name": categoria[1]} if categoria else None,
            "user_recipient": {"id": 5000, "name": solicitante},
            "date_creation": datetime.now(UTC).isoformat(),
            "team": [],
            "actiontime": 0,
            "is_deleted": False,
        }
        self.followups.setdefault(ticket_id, [])

    def adicionar_followup(self, ticket_id: int, autor_id: int, autor_login: str, texto: str) -> None:
        self.followups.setdefault(ticket_id, []).append(
            {
                "type": "Followup",
                "item": {
                    "user": {"id": autor_id, "name": autor_login},
                    "content": texto,
                    "date_creation": datetime.now(UTC).isoformat(),
                },
            }
        )

    def status(self, ticket_id: int) -> int:
        return self.tickets[ticket_id]["status"]["id"]

    def atribuidos(self, ticket_id: int) -> list[int]:
        return [m["id"] for m in self.tickets[ticket_id]["team"] if m["type"] == "User"]


def criar_glpi_falso(estado: EstadoGLPI) -> ServidorFalso:
    async def _comum() -> None:
        estado.requisicoes += 1
        if estado.travar:
            await asyncio.sleep(_TRAVADO_SEGUNDOS)
        if estado.atraso_segundos:
            await asyncio.sleep(estado.atraso_segundos)

    async def token(_request: Request):
        await _comum()
        return JSONResponse({"access_token": "token-falso", "expires_in": 3600})

    async def listar_tickets(request: Request):
        await _comum()
        filtro = request.query_params.get("filter", "")
        status_aceitos = {int(x) for x in re.findall(r"status\.id==(\d+)", filtro)}
        id_maior_que = re.search(r"id=gt=(\d+)", filtro)
        minimo = int(id_maior_que.group(1)) if id_maior_que else 0
        itens = [
            t
            for t in sorted(estado.tickets.values(), key=lambda t: t["id"])
            if t["id"] > minimo and (not status_aceitos or t["status"]["id"] in status_aceitos)
        ]
        return JSONResponse(itens)

    async def ticket(request: Request):
        await _comum()
        ticket_id = int(request.path_params["id"])
        if ticket_id not in estado.tickets:
            return JSONResponse({"status": "ERROR_ITEM_NOT_FOUND"}, status_code=404)
        if request.method == "PATCH":
            corpo = await request.json()
            if "status" in corpo:
                estado.tickets[ticket_id]["status"] = {"id": corpo["status"]["id"], "name": ""}
            if "category" in corpo:
                estado.tickets[ticket_id]["category"] = {"id": corpo["category"]["id"], "name": ""}
        return JSONResponse(estado.tickets[ticket_id])

    async def followups(request: Request):
        await _comum()
        ticket_id = int(request.path_params["id"])
        if request.method == "POST":
            corpo = await request.json()
            estado.adicionar_followup(ticket_id, estado.conta_ia_id, estado.conta_ia_login, corpo["content"])
            return JSONResponse({"id": len(estado.followups[ticket_id])}, status_code=201)
        return JSONResponse(estado.followups.get(ticket_id, []))

    async def team_member(request: Request):
        await _comum()
        ticket_id = int(request.path_params["id"])
        corpo = await request.json()
        time_atual = estado.tickets[ticket_id]["team"]
        membro = {"type": "User", "id": corpo["id"], "role": "assigned"}
        if request.method == "POST":
            if membro in time_atual:
                # Comportamento real confirmado no GLPI: atribuir a mesma pessoa 2x.
                return JSONResponse({"status": "ERROR_INVALID_PARAMETER"}, status_code=400)
            time_atual.append(membro)
            if estado.tickets[ticket_id]["status"]["id"] == 1:
                estado.tickets[ticket_id]["status"] = {
                    "id": 2,
                    "name": "",
                }  # atribuir muda pra "Em atendimento"
            return JSONResponse({}, status_code=201)
        if membro in time_atual:
            time_atual.remove(membro)
        return JSONResponse({})

    async def pending_reason(request: Request):
        await _comum()
        ticket_id = int(request.path_params["id"])
        if ticket_id in estado.pending_reasons:
            return JSONResponse({"id": 1})
        return JSONResponse({}, status_code=404)

    async def timeline_documentos(request: Request):
        await _comum()
        ticket_id = int(request.path_params["id"])
        return JSONResponse(
            [
                {"type": "Document", "item": {"documents_id": d}}
                for d in estado.documentos_por_ticket.get(ticket_id, [])
            ]
        )

    async def documento_metadado(request: Request):
        await _comum()
        nome, mime, _ = estado.documentos[int(request.path_params["id"])]
        return JSONResponse({"filename": nome, "mime": mime})

    # API Legada (apirest.php) — sessão, PendingReason_Item, download de anexo.
    async def init_session(_request: Request):
        await _comum()
        return JSONResponse({"session_token": "sessao-falsa"})

    async def kill_session(_request: Request):
        return JSONResponse({})

    async def pending_reason_item(request: Request):
        await _comum()
        corpo = await request.json()
        estado.pending_reasons.add(corpo["input"]["items_id"])
        return JSONResponse({"id": 1}, status_code=201)

    async def documento_bruto(request: Request):
        await _comum()
        nome, mime, conteudo = estado.documentos[int(request.path_params["id"])]
        return Response(conteudo, media_type=mime)

    base = "/api.php/v2.3/Assistance/Ticket"
    app = Starlette(
        routes=[
            Route("/api.php/token", token, methods=["POST"]),
            Route(base, listar_tickets, methods=["GET"]),
            Route(base + "/{id:int}", ticket, methods=["GET", "PATCH"]),
            Route(base + "/{id:int}/Timeline/Followup", followups, methods=["GET", "POST"]),
            Route(base + "/{id:int}/Timeline/Document", timeline_documentos, methods=["GET"]),
            Route(base + "/{id:int}/TeamMember", team_member, methods=["POST", "DELETE"]),
            Route(base + "/{id:int}/PendingReason", pending_reason, methods=["GET"]),
            Route("/api.php/v2.3/Management/Document/{id:int}", documento_metadado, methods=["GET"]),
            Route("/apirest.php/initSession", init_session, methods=["GET"]),
            Route("/apirest.php/killSession", kill_session, methods=["GET"]),
            Route("/apirest.php/PendingReason_Item", pending_reason_item, methods=["POST"]),
            Route("/apirest.php/Document/{id:int}", documento_bruto, methods=["GET"]),
        ]
    )
    return ServidorFalso(app)


# ----------------------------------------------------------------- OCI ---


@dataclass
class EstadoOCI:
    # Chat (endpoint compatível com OpenAI)
    chat_modo: str = "ok"  # "ok" | "travar" | "erro_500" | "erro_429"
    chat_atraso_segundos: float = 0.0
    chat_requisicoes: int = 0
    # Embedding (SDK nativo)
    embed_modo: str = "ok"  # "ok" | "travar" | "erro_500"
    embed_atraso_segundos: float = 0.0
    embed_requisicoes: int = 0
    embed_assinadas: int = 0  # requisições que chegaram com assinatura RSA da OCI


def _escolha_de_categoria(mensagens: list[dict]) -> dict | None:
    """Pedido de escolha de categoria (`roteamento_chamado._escolher_com_ia`):
    "IA" que sabe que pedido de acesso a views do banco é permissão de banco
    de dados — se essa opção estiver entre as candidatas; senão, a 1ª."""
    texto = mensagens[-1].get("content", "") if mensagens else ""
    if "Categorias possíveis" not in texto:
        return None
    opcoes = re.findall(r"^- (\d+): (.+)$", texto, flags=re.MULTILINE)
    if "views" in texto.lower():
        for categoria_id, nome in opcoes:
            if nome.endswith("Banco de Dados > Permissões"):
                return {"categoria_id": int(categoria_id)}
    return {"categoria_id": int(opcoes[0][0])}


def _decisao_da_ia(mensagens: list[dict]) -> dict:
    escolha = _escolha_de_categoria(mensagens)
    if escolha is not None:
        return escolha
    """ "Julgamento" determinístico: título com `[vago]` e nenhuma resposta
    do solicitante ainda → pede mais informação; senão, suficiente."""
    primeira_do_usuario = next((m for m in mensagens if m.get("role") == "user"), {})
    texto = primeira_do_usuario.get("content", "")
    if isinstance(texto, list):
        texto = " ".join(parte.get("text", "") for parte in texto)
    respostas = [m for m in mensagens if m.get("role") == "user"][1:]
    if "[vago]" in texto and not respostas:
        return {
            "suficiente": False,
            "pergunta": "Qual mensagem de erro aparece na tela?",
            "exemplo": "Erro 0x0000011b ao imprimir",
        }
    return {"suficiente": True, "pergunta": "", "exemplo": ""}


def _vetor_falso(texto: str, dimensao: int = 64) -> list[float]:
    vetor = [0.0] * dimensao
    for palavra in re.findall(r"\w+", texto.lower()):
        vetor[int(hashlib.md5(palavra.encode()).hexdigest(), 16) % dimensao] += 1.0
    norma = math.sqrt(sum(v * v for v in vetor)) or 1.0
    return [v / norma for v in vetor]


def criar_oci_falsa(estado: EstadoOCI) -> ServidorFalso:
    async def _comportamento_chat() -> Response | None:
        estado.chat_requisicoes += 1
        if estado.chat_modo == "travar":
            await asyncio.sleep(_TRAVADO_SEGUNDOS)
        if estado.chat_atraso_segundos:
            await asyncio.sleep(estado.chat_atraso_segundos)
        if estado.chat_modo == "erro_500":
            return JSONResponse({"error": {"message": "Internal error"}}, status_code=500)
        if estado.chat_modo == "erro_429":
            return JSONResponse({"error": {"message": "Too many requests"}}, status_code=429)
        return None

    async def responses(request: Request):
        corpo = await request.json()
        erro = await _comportamento_chat()
        if erro is not None:
            return erro
        texto = json.dumps(_decisao_da_ia(corpo["input"]), ensure_ascii=False)
        return JSONResponse(
            {
                "id": "resp_falsa",
                "object": "response",
                "created_at": int(time.time()),
                "model": corpo["model"],
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_falsa",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": texto, "annotations": []}],
                    }
                ],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 48,
                    "total_tokens": 168,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 31},
                },
            }
        )

    async def chat_completions(request: Request):
        corpo = await request.json()
        erro = await _comportamento_chat()
        if erro is not None:
            return erro
        texto = json.dumps(_decisao_da_ia(corpo["messages"]), ensure_ascii=False)
        return JSONResponse(
            {
                "id": "chatcmpl_falso",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": corpo["model"],
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": texto}}
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 48, "total_tokens": 168},
            }
        )

    async def embed_text(request: Request):
        estado.embed_requisicoes += 1
        if "signature" in request.headers.get("authorization", ""):
            estado.embed_assinadas += 1
        corpo = await request.json()
        if estado.embed_modo == "travar":
            await asyncio.sleep(_TRAVADO_SEGUNDOS)
        if estado.embed_atraso_segundos:
            await asyncio.sleep(estado.embed_atraso_segundos)
        if estado.embed_modo == "erro_500":
            return JSONResponse({"code": "InternalError", "message": "erro"}, status_code=500)
        return JSONResponse(
            {
                "id": "embed_falso",
                "inputs": corpo["inputs"],
                "embeddings": [_vetor_falso(texto) for texto in corpo["inputs"]],
                "modelId": corpo["servingMode"]["modelId"],
                "modelVersion": "1",
            }
        )

    app = Starlette(
        routes=[
            Route("/openai/v1/responses", responses, methods=["POST"]),
            Route("/openai/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/20231130/actions/embedText", embed_text, methods=["POST"]),
        ]
    )
    return ServidorFalso(app)
