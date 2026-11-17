"""Disjuntor ("circuit breaker") pro provedor de IA durante UMA rodada do
poller de triagem (`server/ti/chamados.py`).

Motivo: o timeout de cada chamada (`cliente_protegido.py::
_TIMEOUT_OPENAI_COMPATIVEL`) limita quanto UMA chamada pode ficar presa,
mas, com a OCI fora do ar, cada chamado do lote pagaria esse tempo inteiro
de novo (2 tentativas × 120s). Com 10 chamados na fila, isso é ~40 min de
rodada, o mesmo sintoma de "triagem travada". Depois do primeiro erro de
rede/timeout, o disjuntor "abre" e as próximas chamadas falham NA HORA
(`ProvedorIAIndisponivel`). Quem chama já trata erro de IA caindo no plano
B (`avaliar_chamado` usa a regra de palavras; `classificar_categoria`
mantém a categoria atual), então o resto do lote continua sendo triado sem
esperar. Cada rodada cria um disjuntor novo, ou seja, a IA é tentada de
novo na rodada seguinte (5 min depois).

Só erro de rede/timeout abre o disjuntor. Erro HTTP com resposta (400,
429, 500) volta rápido e não segura o lote, então segue o fluxo normal."""

from collections.abc import Awaitable, Callable

import httpx
import oci
import openai
from oci._vendor import requests as requests_oci

_ERROS_DE_PROVEDOR_INACESSIVEL: tuple[type[BaseException], ...] = (
    TimeoutError,
    httpx.TimeoutException,
    httpx.TransportError,
    openai.APIConnectionError,  # inclui `openai.APITimeoutError`
    oci.exceptions.RequestException,
    oci.exceptions.ConnectTimeout,
    # SDK da OCI usa uma cópia própria (vendorizada) do `requests`.
    requests_oci.exceptions.ConnectionError,
    requests_oci.exceptions.Timeout,
)


class ProvedorIAIndisponivel(Exception):
    """O provedor já falhou por rede/timeout nesta rodada; nem tenta de novo."""


def e_erro_de_provedor_inacessivel(erro: BaseException) -> bool:
    """Olha também a causa encadeada: os SDKs costumam embrulhar o erro de
    rede original (`raise X from erro_de_rede`)."""
    atual: BaseException | None = erro
    vistos = 0
    while atual is not None and vistos < 10:
        if isinstance(atual, _ERROS_DE_PROVEDOR_INACESSIVEL):
            return True
        atual = atual.__cause__ or atual.__context__
        vistos += 1
    return False


class DisjuntorIA:
    """Mesma interface `chat`/`embed` do cliente envolvido."""

    def __init__(self, cliente, ao_abrir: Callable[[str, BaseException], None] | None = None):
        self._cliente = cliente
        self._ao_abrir = ao_abrir
        self.aberto = False

    def abrir(self, operacao: str, erro: BaseException) -> None:
        if self.aberto:
            return
        self.aberto = True
        if self._ao_abrir is not None:
            self._ao_abrir(operacao, erro)

    async def chat(self, **kwargs):
        return await self._chamar("chat", self._cliente.chat, kwargs)

    async def embed(self, **kwargs):
        return await self._chamar("embed", self._cliente.embed, kwargs)

    async def _chamar(self, operacao: str, metodo: Callable[..., Awaitable], kwargs: dict):
        if self.aberto:
            raise ProvedorIAIndisponivel(f"provedor de IA indisponível nesta rodada ({operacao})")
        try:
            return await metodo(**kwargs)
        except Exception as erro:
            if e_erro_de_provedor_inacessivel(erro):
                self.abrir(operacao, erro)
            raise
