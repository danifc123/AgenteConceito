"""Disjuntor ("circuit breaker") pro provedor de IA durante UMA rodada do
poller de triagem (`server/ti/chamados.py`).

Motivo: o timeout de cada chamada (`cliente_protegido.py::
_TIMEOUT_OPENAI_COMPATIVEL`) limita quanto UMA chamada pode ficar presa,
mas, com a OCI fora do ar, cada chamado do lote pagaria esse tempo inteiro
de novo (2 tentativas × 120s). Com 10 chamados na fila, isso é ~40 min de
rodada, o mesmo sintoma de "triagem travada". Depois da primeira falha de
disponibilidade, o disjuntor "abre" e as próximas chamadas falham NA HORA
(`ProvedorIAIndisponivel`), sem rede. Cada rodada cria um disjuntor novo,
ou seja, a IA é tentada de novo na rodada seguinte (5 min depois).

O que fazer com o chamado quando a IA está indisponível é decisão de quem
chama (regra de negócio, decisão do Daniel em 2026-11): por padrão o
poller ADIA o chamado pra próxima rodada, sem mexer nele; só depois de 1h
seguida de IA fora é que volta a decidir pela regra simples (ver
`server/ti/chamados.py::_LIMITE_IA_FORA_ANTES_DA_REGRA`).

"Indisponível" = o provedor não está atendendo: erro de rede, timeout,
erro 5xx ou 429 (limite de requisições). Erro que indica problema na
PRÓPRIA requisição (400, 401, JSON inválido) não abre o disjuntor e segue
o tratamento normal de quem chama."""

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
    """O provedor de IA não está atendendo (nesta rodada, ou o disjuntor já abriu)."""


def _status_http(erro: BaseException) -> int | None:
    if isinstance(erro, openai.APIStatusError):
        return erro.status_code
    if isinstance(erro, oci.exceptions.ServiceError):
        return erro.status
    if isinstance(erro, httpx.HTTPStatusError):
        return erro.response.status_code
    return None


def e_erro_de_provedor_inacessivel(erro: BaseException) -> bool:
    """Olha também a causa encadeada: os SDKs costumam embrulhar o erro de
    rede original (`raise X from erro_de_rede`)."""
    atual: BaseException | None = erro
    vistos = 0
    while atual is not None and vistos < 10:
        if isinstance(atual, (ProvedorIAIndisponivel, *_ERROS_DE_PROVEDOR_INACESSIVEL)):
            return True
        status = _status_http(atual)
        if status is not None and (status >= 500 or status == 429):
            return True
        atual = atual.__cause__ or atual.__context__
        vistos += 1
    return False


class DisjuntorIA:
    """Mesma interface `chat`/`embed` do cliente envolvido. `sucessos` e
    `ultimo_erro` alimentam o estado "IA fora do ar desde…" do poller."""

    def __init__(self, cliente, ao_abrir: Callable[[str, BaseException], None] | None = None):
        self._cliente = cliente
        self._ao_abrir = ao_abrir
        self.aberto = False
        self.sucessos = 0
        self.ultimo_erro: str | None = None

    def abrir(self, operacao: str, erro: BaseException) -> None:
        if self.aberto:
            return
        self.aberto = True
        self.ultimo_erro = f"{type(erro).__name__}: {erro}"[:300]
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
            resposta = await metodo(**kwargs)
        except Exception as erro:
            if e_erro_de_provedor_inacessivel(erro):
                self.abrir(operacao, erro)
                raise ProvedorIAIndisponivel(f"provedor de IA não respondeu ({operacao})") from erro
            raise
        self.sucessos += 1
        return resposta
