"""Cenários de produção do poller de triagem, ponta a ponta.

Roda `_executar_uma_rodada`/`iniciar_poller_verificar_chamados` de verdade
com os clientes REAIS: `ClienteGLPIReal` (httpx, token OAuth, API Legada),
`AsyncOpenAI` com os timeouts de produção (via `construir_cliente_llm`), o
SDK nativo da OCI (assinatura RSA de verdade) pro embedding e o Postgres de
teste (uso de IA, amostragem, configuração). Só o GLPI e a OCI são falsos,
servidores HTTP reais em socket TCP (`_servidores_falsos.py`), pra dar pra
simular provedor lento, travado ou com erro.

Escala de tempo: todos os limites são os de produção divididos por ~60
(120s de timeout da IA viram 2s, 420s por chamado viram 7s etc.), pra suíte
rodar em segundos. A proporção entre eles é a mesma de produção. Cada teste
imprime quanto a rodada levou (`-s` pra ver)."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import oci
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agente_oracle.agent.ti import roteamento_chamado
from agente_oracle.config import settings
from agente_oracle.db.connection import get_postgres_connection
from agente_oracle.server.ti import chamados as chamados_module
from agente_oracle.tools.ia import auditoria_externa, cliente_protegido
from agente_oracle.tools.ia.provedores_llm import ProvedorLLM
from agente_oracle.tools.ti import amostragem_chamados, anexos_chamado, glpi, uso_ia_chamados
from agente_oracle.tools.ti.categorias import CATEGORIAS_ATRIBUIVEIS

from ._servidores_falsos import EstadoGLPI, EstadoOCI, criar_glpi_falso, criar_oci_falsa

pytestmark = pytest.mark.integration

_ESCALA = 60  # 1s no teste = 60s em produção
_ID_BASE = 900_100_000
_CONTA_IA_ID = 999

_CATEGORIA_IMPRESSORA = next((c.id, c.nome) for c in CATEGORIAS_ATRIBUIVEIS if "Impressoras" in c.nome)
_CATEGORIA_HARDWARE = (CATEGORIAS_ATRIBUIVEIS[0].id, CATEGORIAS_ATRIBUIVEIS[0].nome)
_CATEGORIA_SISTEMAS = next((c.id, c.nome) for c in CATEGORIAS_ATRIBUIVEIS if c.area == "sistemas")

_ROSTER = [
    {"usuario": "ana", "nome": "Ana Infra", "tecnico_glpi_id": "101", "area_ti": "infra"},
    {"usuario": "bruno", "nome": "Bruno Infra", "tecnico_glpi_id": "102", "area_ti": "infra"},
    {"usuario": "carla", "nome": "Carla Sistemas", "tecnico_glpi_id": "201", "area_ti": "sistemas"},
    {"usuario": "diego", "nome": "Diego Processos", "tecnico_glpi_id": "301", "area_ti": "processos"},
]
_IDS_TECNICOS = {int(t["tecnico_glpi_id"]) for t in _ROSTER}

_DESCRICAO_DETALHADA = (
    "<p>Desde ontem a impressora HP do financeiro, no 2º andar, mostra o erro 0x0000011b ao "
    "imprimir qualquer documento pelo Protheus. Já reiniciei o computador e a impressora.</p>"
)


def _chave_privada_pem() -> str:
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return chave.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def _provedor(**campos) -> ProvedorLLM:
    base = {
        "id": 1,
        "nome": "OCI",
        "api_key": "chave-falsa",
        "projeto_id": "",
        "preco_entrada_por_1k": Decimal("0"),
        "preco_saida_por_1k": Decimal("0"),
        "moeda": "USD",
        "credenciais_extra": None,
        "credencial_atualizada_em": datetime.now(UTC),
        "criado_em": datetime.now(UTC),
    }
    return ProvedorLLM(**{**base, **campos})


def _limpar_postgres() -> None:
    uso_ia_chamados.resumo_uso(1)  # garante as tabelas
    amostragem_chamados.ids_fora_da_amostra()
    auditoria_externa.contagem_hoje("ti")
    with get_postgres_connection() as connection:
        cursor = connection.cursor()
        cursor.execute("DELETE FROM ti_uso_ia_chamados WHERE chamado_id >= :base", base=_ID_BASE)
        cursor.execute("DELETE FROM ti_amostragem_chamados WHERE chamado_id >= :base", base=_ID_BASE)
        cursor.execute("DELETE FROM auditoria_ia_externa WHERE host LIKE 'http://127.0.0.1:%'")


class Ambiente:
    def __init__(self, glpi_estado: EstadoGLPI, oci_estado: EstadoOCI):
        self.glpi = glpi_estado
        self.oci = oci_estado
        self._proximo_id = _ID_BASE

    def chamado(
        self, titulo: str, descricao: str = _DESCRICAO_DETALHADA, categoria=_CATEGORIA_IMPRESSORA
    ) -> int:
        self._proximo_id += 1
        self.glpi.adicionar_ticket(self._proximo_id, titulo, descricao, categoria)
        return self._proximo_id

    async def rodada(self) -> float:
        inicio = time.monotonic()
        await chamados_module._executar_uma_rodada()
        return time.monotonic() - inicio

    def etapas(self) -> dict[str, dict]:
        return {e["id"]: e for e in chamados_module._estado_poller_para_json()["etapas"]}

    def log(self) -> str:
        return "\n".join(chamados_module._historico_log)

    def foi_liberado_pra_tecnico(self, ticket_id: int) -> bool:
        atribuidos = self.glpi.atribuidos(ticket_id)
        return (
            self.glpi.status(ticket_id) == 2
            and _CONTA_IA_ID not in atribuidos
            and any(a in _IDS_TECNICOS for a in atribuidos)
        )

    def esta_intocado(self, ticket_id: int) -> bool:
        """Do jeito que chegou: status Novo, ninguém atribuído, sem comentário."""
        return (
            self.glpi.status(ticket_id) == 1
            and self.glpi.atribuidos(ticket_id) == []
            and self.glpi.followups[ticket_id] == []
        )

    def esta_aguardando_usuario(self, ticket_id: int) -> bool:
        return self.glpi.status(ticket_id) == 4 and _CONTA_IA_ID in self.glpi.atribuidos(ticket_id)

    def ultima_mensagem_da_ia(self, ticket_id: int) -> str:
        da_ia = [
            f["item"]["content"]
            for f in self.glpi.followups[ticket_id]
            if f["item"]["user"]["id"] == _CONTA_IA_ID
        ]
        return da_ia[-1] if da_ia else ""


def _relatar(nome: str, segundos: float, detalhe: str = "") -> None:
    """Tempo bruto do teste. Só os LIMITES estão em escala (÷60); o custo
    fixo (assinatura RSA da OCI, conexões, Postgres) não, então isso aqui
    não se converte direto pra minutos de produção."""
    print(f"\n[cenário] {nome}: {segundos:.1f}s. {detalhe}")


@pytest.fixture
def ambiente(monkeypatch):
    glpi_estado = EstadoGLPI(conta_ia_id=_CONTA_IA_ID, conta_ia_login="conta.ia")
    oci_estado = EstadoOCI()
    servidor_glpi = criar_glpi_falso(glpi_estado).iniciar()
    servidor_oci = criar_oci_falsa(oci_estado).iniciar()

    # Configuração do GLPI apontando pro servidor falso.
    monkeypatch.setattr(settings, "glpi_base_url", servidor_glpi.url)
    monkeypatch.setattr(settings, "glpi_client_id", "id")
    monkeypatch.setattr(settings, "glpi_client_secret", "segredo")
    monkeypatch.setattr(settings, "glpi_username", "conta.ia")
    monkeypatch.setattr(settings, "glpi_password", "senha")
    monkeypatch.setattr(settings, "glpi_conta_ia_id", str(_CONTA_IA_ID))
    monkeypatch.setattr(settings, "glpi_legacy_api_url", f"{servidor_glpi.url}/apirest.php")
    monkeypatch.setattr(settings, "glpi_legacy_app_token", "app")
    monkeypatch.setattr(settings, "glpi_legacy_user_token", "user")

    # Limites de produção ÷ 60.
    monkeypatch.setattr(glpi, "_TIMEOUT_HTTP_SEGUNDOS", 10.0 / _ESCALA * 3)  # 0.5s
    monkeypatch.setattr(glpi, "_ESPERA_ENTRE_TENTATIVAS_SEGUNDOS", 1.0 / _ESCALA * 6)
    monkeypatch.setattr(
        cliente_protegido, "_TIMEOUT_OPENAI_COMPATIVEL", httpx.Timeout(120 / _ESCALA, connect=1.0)
    )
    monkeypatch.setattr(cliente_protegido, "_TIMEOUT_OCI_NATIVO", (1, 60 / _ESCALA))
    monkeypatch.setattr(chamados_module, "_TIMEOUT_POR_CHAMADO_SEGUNDOS", 420 / _ESCALA)
    monkeypatch.setattr(chamados_module, "_TIMEOUT_AQUECIMENTO_EMBEDDINGS_SEGUNDOS", 600 / _ESCALA)
    monkeypatch.setattr(chamados_module, "_INTERVALO_POLLER_SEGUNDOS", 300 / _ESCALA)
    monkeypatch.setattr(anexos_chamado, "_TIMEOUT_EXTRACAO_SEGUNDOS", 30 / _ESCALA)

    monkeypatch.setattr(chamados_module, "_cliente", glpi.ClienteGLPIReal(settings))

    # Provedores cadastrados como em produção: chat OCI pelo endpoint
    # compatível com OpenAI (gpt-oss-120b → Responses API) e embedding pelo
    # SDK nativo da OCI.
    chat = _provedor(
        tipo_conexao="openai_compativel",
        base_url=f"{servidor_oci.url}/openai/v1",
        modelo="openai.gpt-oss-120b",
        estilo_api="responses",
        capacidades=["chat"],
    )
    embedding = _provedor(
        id=2,
        nome="OCI embedding",
        tipo_conexao="oci_nativo",
        base_url="",
        modelo="cohere.embed-multilingual-v3.0",
        estilo_api="chat_completions",
        capacidades=["embedding"],
        credenciais_extra={
            "tenancy_ocid": "ocid1.tenancy.oc1..aaaaaaaateste",
            "user_ocid": "ocid1.user.oc1..aaaaaaaateste",
            "fingerprint": "11:22:33:44:55:66:77:88:99:00:aa:bb:cc:dd:ee:ff",
            "chave_privada": _chave_privada_pem(),
            "regiao": "sa-saopaulo-1",
            "compartment_id": "ocid1.compartment.oc1..aaaaaaaateste",
        },
    )
    monkeypatch.setattr(cliente_protegido, "_provedor_llm_ativo", lambda _dominio: chat)
    monkeypatch.setattr(cliente_protegido, "_provedor_llm_embedding_ativo", lambda _dominio: embedding)
    cliente_oci_original = oci.generative_ai_inference.GenerativeAiInferenceClient

    def _cliente_oci_no_servidor_falso(*args, **kwargs):
        kwargs["service_endpoint"] = servidor_oci.url
        return cliente_oci_original(*args, **kwargs)

    monkeypatch.setattr(
        oci.generative_ai_inference, "GenerativeAiInferenceClient", _cliente_oci_no_servidor_falso
    )

    monkeypatch.setattr("agente_oracle.tools.ti.tecnicos.listar_tecnicos_ti", lambda: _ROSTER)
    # Processo recém-reiniciado (deploy): cache de embedding das categorias vazio.
    monkeypatch.setattr(roteamento_chamado, "_cache_embeddings_categorias", None)
    chamados_module._estado_poller = chamados_module.EstadoPoller()
    chamados_module._historico_log.clear()
    _limpar_postgres()

    yield Ambiente(glpi_estado, oci_estado)

    servidor_glpi.parar()
    servidor_oci.parar()
    _limpar_postgres()
    chamados_module._estado_poller = chamados_module.EstadoPoller()


async def test_dia_normal_lote_misto_logo_depois_do_deploy(ambiente):
    detalhados = [
        ambiente.chamado("Impressora do financeiro com erro 0x0000011b"),
        ambiente.chamado("Notebook não liga após queda de energia", categoria=_CATEGORIA_HARDWARE),
        ambiente.chamado("Erro ao gerar NF no Protheus", categoria=_CATEGORIA_SISTEMAS),
        ambiente.chamado("Chamado aberto por e-mail", categoria=None),  # sem categoria
        ambiente.chamado("Monitor piscando na recepção", categoria=_CATEGORIA_HARDWARE),
    ]
    vagos = [
        ambiente.chamado("[vago] impressora", descricao="<p>a impressora não imprime, me ajuda por favor</p>")
        for _ in range(2)
    ]
    ambiente.oci.chat_atraso_segundos = 0.1
    ambiente.oci.embed_atraso_segundos = 0.005

    duracao = await ambiente.rodada()
    _relatar("dia normal (7 chamados, logo após deploy)", duracao)

    assert all(ambiente.foi_liberado_pra_tecnico(t) for t in detalhados)
    assert all(ambiente.esta_aguardando_usuario(t) for t in vagos)
    assert all(t in ambiente.glpi.pending_reasons for t in vagos)
    assert "Qual mensagem de erro aparece na tela?" in ambiente.ultima_mensagem_da_ia(vagos[0])  # veio da IA
    assert (
        ambiente.glpi.tickets[detalhados[3]]["category"] is not None
    )  # IA escolheu categoria pro sem categoria
    assert ambiente.etapas()["chamados_novos"] == {
        **ambiente.etapas()["chamados_novos"],
        "status": "concluido",
    }
    assert ambiente.etapas()["chamados_novos"]["processados"] == 7
    assert (
        ambiente.oci.embed_assinadas == ambiente.oci.embed_requisicoes == 2 + 5
    )  # 2 lotes + 1 por chamado suficiente (vago não classifica)
    assert all(uso_ia_chamados.ultima_avaliacao(t) is not None for t in detalhados + vagos)

    # Solicitante responde um dos vagos → próxima rodada reavalia e libera.
    ambiente.glpi.adicionar_followup(
        vagos[0], 5000, "joao.silva", "Aparece o erro 0x0000011b ao imprimir o boleto"
    )
    requisicoes_embed_antes = ambiente.oci.embed_requisicoes

    duracao = await ambiente.rodada()
    _relatar("rodada seguinte (1 resposta do solicitante)", duracao)

    assert ambiente.foi_liberado_pra_tecnico(vagos[0])
    assert ambiente.esta_aguardando_usuario(vagos[1])  # esse ninguém respondeu: fica como está
    assert ambiente.etapas()["chamados_aguardando_resposta"]["processados"] == 1
    # Cache das categorias reaproveitado: só o embedding do chamado reavaliado.
    assert ambiente.oci.embed_requisicoes - requisicoes_embed_antes == 1


async def test_oci_chat_travada_adia_o_lote_sem_mexer_nos_chamados(ambiente):
    """O cenário dos 30 minutos: a OCI aceita a conexão e nunca responde.
    Regra de negócio: IA fora → chamado adiado, intocado no GLPI."""
    chamados = [ambiente.chamado(f"Impressora {i} com erro 0x0000011b") for i in range(6)]
    chamados.append(ambiente.chamado("[vago] impressora", descricao="<p>a impressora não imprime nada</p>"))
    ambiente.oci.chat_modo = "travar"

    duracao = await ambiente.rodada()
    _relatar("OCI chat travada (7 chamados)", duracao, f"requisições de chat: {ambiente.oci.chat_requisicoes}")

    # Só o 1º chamado espera (1 tentativa + 1 retry de 2s); o resto é adiado na hora.
    assert ambiente.oci.chat_requisicoes == 2
    assert duracao < 2 * (120 / _ESCALA) + 3
    assert all(ambiente.esta_intocado(t) for t in chamados)
    assert "7 chamado(s) adiado(s)" in ambiente.log()
    estado = chamados_module._estado_poller_para_json()["ia"]
    assert estado["indisponivel_desde"] is not None and estado["modo_regra"] is False

    # OCI volta: a rodada seguinte tria todo mundo com a IA e limpa o aviso.
    ambiente.oci.chat_modo = "ok"
    await ambiente.rodada()
    assert all(ambiente.foi_liberado_pra_tecnico(t) for t in chamados[:6])
    assert ambiente.esta_aguardando_usuario(chamados[6])
    assert "Qual mensagem de erro aparece na tela?" in ambiente.ultima_mensagem_da_ia(chamados[6])
    assert chamados_module._estado_poller_para_json()["ia"]["indisponivel_desde"] is None


async def test_oci_fora_por_mais_de_1h_volta_a_decidir_pela_regra(ambiente):
    chamados = [ambiente.chamado(f"Impressora {i} com erro 0x0000011b") for i in range(3)]
    ambiente.oci.chat_modo = "travar"
    chamados_module._estado_poller.ia_indisponivel_desde = datetime.now(UTC) - timedelta(minutes=61)

    duracao = await ambiente.rodada()
    _relatar("OCI fora há mais de 1h", duracao)

    assert all(ambiente.foi_liberado_pra_tecnico(t) for t in chamados)  # regra: descrição detalhada
    assert chamados_module._estado_poller_para_json()["ia"]["modo_regra"] is True
    assert "regra simples" in ambiente.log()


async def test_oci_chat_lenta_mas_dentro_do_limite_continua_usando_a_ia(ambiente):
    """Modelo de raciocínio respondendo em ~90s (1.5s no teste): não pode cair no plano B."""
    vago = ambiente.chamado("[vago] impressora", descricao="<p>a impressora não imprime nada hoje cedo</p>")
    ambiente.oci.chat_atraso_segundos = 90 / _ESCALA

    duracao = await ambiente.rodada()
    _relatar("OCI chat lenta (90s por resposta)", duracao)

    assert ambiente.oci.chat_requisicoes == 1
    assert "Qual mensagem de erro aparece na tela?" in ambiente.ultima_mensagem_da_ia(vago)
    assert "não respondeu" not in ambiente.log()


@pytest.mark.parametrize("modo", ["erro_500", "erro_429"])
async def test_oci_chat_devolvendo_erro_adia_rapido(ambiente, modo):
    chamados = [ambiente.chamado(f"Impressora {i} com erro 0x0000011b") for i in range(4)]
    ambiente.oci.chat_modo = modo

    duracao = await ambiente.rodada()
    _relatar(f"OCI chat devolvendo {modo}", duracao)

    assert duracao < 15
    assert ambiente.oci.chat_requisicoes == 2  # 1 + 1 retry do SDK, depois o disjuntor corta
    assert all(ambiente.esta_intocado(t) for t in chamados)


async def test_embedding_oci_travado_logo_depois_do_deploy(ambiente):
    detalhados = [ambiente.chamado(f"Impressora {i} com erro 0x0000011b") for i in range(5)]
    categorias_antes = {t: ambiente.glpi.tickets[t]["category"]["id"] for t in detalhados}
    ambiente.oci.embed_modo = "travar"

    duracao = await ambiente.rodada()
    _relatar("embedding OCI travado no aquecimento pós-deploy", duracao)

    assert ambiente.oci.embed_requisicoes == 1  # 1ª chamada estoura; o disjuntor corta o resto
    assert duracao < 60 / _ESCALA + 3
    assert all(ambiente.esta_intocado(t) for t in detalhados)  # adiados: sem categoria certa não libera
    assert {t: ambiente.glpi.tickets[t]["category"]["id"] for t in detalhados} == categorias_antes
    assert "IA de categoria (embedding) não respondeu" in ambiente.log()

    ambiente.oci.embed_modo = "ok"
    await ambiente.rodada()
    assert all(ambiente.foi_liberado_pra_tecnico(t) for t in detalhados)


async def test_embedding_lento_mas_dentro_do_limite(ambiente):
    """Cada chamada de embedding levando ~40s em produção (limite: 60s): o
    aquecimento pós-deploy (2 lotes) e a correção de categoria funcionam."""
    sem_categoria = ambiente.chamado("Chamado aberto por e-mail", categoria=None)
    outros = [ambiente.chamado(f"Impressora {i} com erro 0x0000011b") for i in range(2)]
    ambiente.oci.embed_atraso_segundos = 40 / _ESCALA

    duracao = await ambiente.rodada()
    _relatar(
        "embedding lento (~40s por chamada)",
        duracao,
        f"requisições de embedding: {ambiente.oci.embed_requisicoes}",
    )

    assert ambiente.oci.embed_requisicoes == 2 + 3  # 2 lotes de categorias + 1 por chamado
    assert all(ambiente.foi_liberado_pra_tecnico(t) for t in [sem_categoria, *outros])
    assert ambiente.glpi.tickets[sem_categoria]["category"] is not None
    assert "pulado" not in ambiente.log()
    assert "não respondeu" not in ambiente.log()


async def test_glpi_travado_encerra_a_rodada_e_a_seguinte_funciona(ambiente):
    chamado = ambiente.chamado("Impressora com erro 0x0000011b")
    ambiente.glpi.travar = True

    duracao = await ambiente.rodada()
    _relatar("GLPI travado", duracao)

    assert duracao < 10
    assert ambiente.etapas()["chamados_novos"]["status"] == "erro"
    assert ambiente.etapas()["chamados_aguardando_resposta"]["status"] == "erro"

    ambiente.glpi.travar = False
    await ambiente.rodada()
    assert ambiente.foi_liberado_pra_tecnico(chamado)


async def test_poller_em_loop_sobrevive_a_glpi_fora_do_ar_e_volta_sozinho(ambiente, monkeypatch):
    """Loop real (`iniciar_poller_verificar_chamados`), GLPI recusando conexão
    nas primeiras rodadas, como num restart do GLPI durante o expediente."""
    chamado = ambiente.chamado("Impressora com erro 0x0000011b")
    cliente_real = chamados_module._cliente
    configuracao_fora = settings.model_copy(update={"glpi_base_url": "http://127.0.0.1:1"})
    monkeypatch.setattr(chamados_module, "_cliente", glpi.ClienteGLPIReal(configuracao_fora))

    tarefa = asyncio.create_task(chamados_module.iniciar_poller_verificar_chamados())
    try:
        await asyncio.sleep(chamados_module._INTERVALO_POLLER_SEGUNDOS * 2.5)
        assert not tarefa.done()  # não morreu
        assert ambiente.etapas()["chamados_novos"]["status"] == "erro"

        chamados_module._cliente = cliente_real  # GLPI voltou
        limite = time.monotonic() + chamados_module._INTERVALO_POLLER_SEGUNDOS * 3
        while not ambiente.foi_liberado_pra_tecnico(chamado) and time.monotonic() < limite:
            await asyncio.sleep(0.2)
        assert ambiente.foi_liberado_pra_tecnico(chamado)
        assert not tarefa.done()
    finally:
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa


async def test_anexos_problematicos_nao_travam_a_triagem(ambiente):
    chamado = ambiente.chamado("[vago] erro no sistema", descricao="<p>dá erro, segue anexo</p>")
    outro = ambiente.chamado("Impressora com erro 0x0000011b")
    ambiente.glpi.documentos = {
        1: ("manual_gigante.pdf", "application/pdf", b"%PDF-1.4\n" + b"0" * (11 * 1024 * 1024)),
        2: ("corrompido.pdf", "application/pdf", b"%PDF-1.7\n isso nao e um pdf de verdade"),
        3: ("erro.log", "text/plain", b"ERRO FATAL: conexao recusada com o servidor de licencas"),
        4: ("print.png", "image/png", b"\x89PNG\r\n"),
    }
    ambiente.glpi.documentos_por_ticket[chamado] = [1, 2, 3, 4]

    duracao = await ambiente.rodada()
    _relatar("anexos problemáticos", duracao)

    assert ambiente.etapas()["chamados_novos"]["processados"] == 2
    assert ambiente.foi_liberado_pra_tecnico(outro)
    assert ambiente.esta_aguardando_usuario(chamado)


async def test_volume_de_um_dia_cheio(ambiente):
    """25 chamados de uma vez (ex: segunda de manhã) com a IA levando ~30s por avaliação."""
    chamados = [ambiente.chamado(f"Impressora {i} com erro 0x0000011b") for i in range(25)]
    ambiente.oci.chat_atraso_segundos = 30 / _ESCALA
    ambiente.oci.embed_atraso_segundos = 0.005

    duracao = await ambiente.rodada()
    _relatar("25 chamados, IA a ~30s por chamado", duracao)

    assert all(ambiente.foi_liberado_pra_tecnico(t) for t in chamados)
    assert "pulado" not in ambiente.log()


async def test_pedido_de_acesso_ao_banco_que_cita_power_bi_vai_pra_categoria_certa(ambiente):
    """Caso real: pedido de acesso de leitura a views do banco via Excel,
    aberto em "Power B.I > Dúvida" porque o texto cita Power BI como
    comparação. A IA recebe os grupos mais próximos inteiros e escolhe."""
    power_bi_duvida = next((c.id, c.nome) for c in CATEGORIAS_ATRIBUIVEIS if c.nome.endswith("Power B.I > Dúvida"))
    chamado = ambiente.chamado(
        "Solicitação de acesso somente leitura a views do banco via Excel (Power Query)",
        descricao=(
            "<p>Solicito a liberação de conexão do Excel (Power Query/ODBC) a views específicas do banco de "
            "dados, somente leitura (SELECT), com atualização em tempo real. A atualização agendada do Power BI "
            "não acompanha o ritmo da expedição. Views: Kardex, Detalhamento OP, SFT, Lançamentos Contábeis, "
            "Margem. Usuários: Driele, Vinicius, Leandro, Dener.</p>"
        ),
        categoria=power_bi_duvida,
    )

    await ambiente.rodada()

    categoria_final = ambiente.glpi.tickets[chamado]["category"]["id"]
    nome_final = next(c.nome for c in CATEGORIAS_ATRIBUIVEIS if c.id == categoria_final)
    _relatar("pedido de acesso ao banco citando Power BI", 0, f"categoria final: {nome_final}")
    assert nome_final.endswith("Banco de Dados > Permissões")
    assert ambiente.foi_liberado_pra_tecnico(chamado)
