import httpx
import openai
import pytest
from oci._vendor import requests as requests_oci

from agente_oracle.tools.ia.disjuntor import (
    DisjuntorIA,
    ProvedorIAIndisponivel,
    e_erro_de_provedor_inacessivel,
)


class _ClienteFake:
    def __init__(self, erro: Exception | None = None):
        self._erro = erro
        self.chamadas = 0

    async def chat(self, **_kwargs):
        self.chamadas += 1
        if self._erro:
            raise self._erro
        return "resposta"

    async def embed(self, **_kwargs):
        self.chamadas += 1
        if self._erro:
            raise self._erro
        return "vetor"


def _timeout_openai() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=httpx.Request("POST", "http://oci/responses"))


class TestErroDeProvedorInacessivel:
    @pytest.mark.parametrize(
        "erro",
        [
            TimeoutError(),
            httpx.ReadTimeout("lento"),
            httpx.ConnectError("recusado"),
            requests_oci.exceptions.ReadTimeout("lento"),
            requests_oci.exceptions.ConnectionError("recusado"),
        ],
    )
    def test_rede_e_timeout_contam(self, erro):
        assert e_erro_de_provedor_inacessivel(erro)

    def test_timeout_do_sdk_openai_conta(self):
        assert e_erro_de_provedor_inacessivel(_timeout_openai())

    def test_causa_encadeada_conta(self):
        try:
            try:
                raise httpx.ReadTimeout("lento")
            except httpx.ReadTimeout as original:
                raise RuntimeError("embrulhado") from original
        except RuntimeError as embrulhado:
            assert e_erro_de_provedor_inacessivel(embrulhado)

    @pytest.mark.parametrize("status", [500, 502, 503, 429])
    def test_5xx_e_429_contam(self, status):
        resposta = httpx.Response(status, request=httpx.Request("POST", "http://oci/responses"))
        erro = openai.APIStatusError("erro", response=resposta, body=None)
        assert e_erro_de_provedor_inacessivel(erro)

    @pytest.mark.parametrize("status", [400, 401, 404])
    def test_erro_da_propria_requisicao_nao_conta(self, status):
        resposta = httpx.Response(status, request=httpx.Request("POST", "http://oci/responses"))
        erro = openai.APIStatusError("erro", response=resposta, body=None)
        assert not e_erro_de_provedor_inacessivel(erro)

    @pytest.mark.parametrize("erro", [ValueError("json"), RuntimeError("500 Internal Error")])
    def test_erro_sem_status_nao_conta(self, erro):
        assert not e_erro_de_provedor_inacessivel(erro)


class TestDisjuntorIA:
    async def test_repassa_resposta_quando_tudo_bem(self):
        cliente = _ClienteFake()
        disjuntor = DisjuntorIA(cliente)
        assert await disjuntor.chat(model="m", messages=[]) == "resposta"
        assert await disjuntor.embed(model="m", input="x") == "vetor"
        assert not disjuntor.aberto
        assert disjuntor.sucessos == 2

    async def test_timeout_abre_e_proximas_chamadas_nem_saem(self):
        cliente = _ClienteFake(erro=_timeout_openai())
        avisos = []
        disjuntor = DisjuntorIA(cliente, ao_abrir=lambda operacao, erro: avisos.append(operacao))

        with pytest.raises(ProvedorIAIndisponivel) as primeira:
            await disjuntor.chat(model="m", messages=[])
        assert isinstance(primeira.value.__cause__, openai.APITimeoutError)
        with pytest.raises(ProvedorIAIndisponivel):
            await disjuntor.chat(model="m", messages=[])
        with pytest.raises(ProvedorIAIndisponivel):
            await disjuntor.embed(model="m", input="x")

        assert cliente.chamadas == 1
        assert avisos == ["chat"]  # avisa uma vez só

    async def test_erro_comum_nao_abre(self):
        cliente = _ClienteFake(erro=ValueError("resposta inválida"))
        disjuntor = DisjuntorIA(cliente)

        for _ in range(3):
            with pytest.raises(ValueError):
                await disjuntor.chat(model="m", messages=[])

        assert cliente.chamadas == 3
        assert not disjuntor.aberto
