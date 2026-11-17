# Deploy do backend com o poller de triagem (GLPI)

Checklist para subir uma versão nova do backend sem travar a triagem de chamados.

## 1. Build da imagem

A imagem instala as dependências pelo `uv.lock` (`uv sync --frozen`), ou seja,
exatamente as versões testadas. Se alguém mudou o `pyproject.toml` sem atualizar
o lockfile, o build falha de propósito. Nesse caso, rode `uv lock`, rode os
testes e faça commit do `uv.lock` antes de buildar de novo.

```bash
docker compose build backend
```

## 2. Um único processo de backend

O poller roda dentro do processo do backend. Cada processo a mais roda o próprio
poller, e dois pollers triando a mesma fila geram atribuição e mensagem
duplicadas no GLPI.

- Nada de `--workers` no uvicorn e nada de `deploy.replicas > 1` no compose.
- **Não deixe dois backends ligados ao mesmo GLPI ao mesmo tempo.** Exemplo: o
  container do compose e um `uv run agente-oracle` local, que é o backend que o
  `ng serve` (porta 4200) usa via `proxy.conf.json`, em `localhost:8000`. Para
  desenvolver com o frontend em modo dev enquanto o container estiver rodando,
  deixe o `GLPI_BASE_URL` vazio no `.env` do backend local: sem ele, o poller
  daquele processo nem inicia.

## 3. Subir

```bash
docker compose up -d backend
```

Depois de cada restart, a primeira rodada gera o embedding das categorias (2
chamadas em lote para a OCI) antes de triar os chamados. Isso é esperado.

## 4. Conferir em até 5 minutos

Auditoria de Chamados > painel **Poller do GLPI** > **Ver log detalhado**:

- Deve aparecer `Rodada iniciada` → `Rodada concluída`, com "Última rodada"
  preenchida e a contagem regressiva para a próxima.
- `IA de avaliação não respondeu…`: a OCI não respondeu dentro do limite (120s,
  com 1 nova tentativa). A rodada segue sem esperar o resto e a IA é tentada de
  novo na próxima.
- `passou de 420s sem resposta … pulado nesta rodada`: um chamado específico
  travou e é tentado de novo na rodada seguinte. Se o mesmo número aparecer
  sempre, investigue esse chamado (anexo, texto enorme etc.).

Pelo terminal:

```bash
docker compose logs -f backend | grep -iE "poller|chamado|IA"
```

## 5. Quanto tempo a IA leva por chamado

Os chamados são triados um por vez. O tempo de uma rodada é, aproximadamente,
o número de chamados × o tempo de resposta do modelo. Para ver o tempo real em
produção:

```sql
-- Tempo por chamado nos últimos 7 dias
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duracao_ms) / 1000 AS mediana_s,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duracao_ms) / 1000 AS p95_s,
       max(duracao_ms) / 1000 AS pior_s,
       count(*) AS chamados
FROM ti_uso_ia_chamados
WHERE criado_em > now() - interval '7 days';

-- Os mais lentos (perto de 1800s = a chamada que ficou presa 30 min)
SELECT chamado_id, duracao_ms / 1000 AS segundos, criado_em
FROM ti_uso_ia_chamados ORDER BY duracao_ms DESC LIMIT 10;
```

Se o p95 chegar perto de 120s, o modelo está respondendo perto do limite.
Aumente `_TIMEOUT_OPENAI_COMPATIVEL` em `tools/ia/cliente_protegido.py` e
`_TIMEOUT_POR_CHAMADO_SEGUNDOS` em `server/ti/chamados.py` na mesma proporção.

## 6. Testes antes do deploy

```bash
uv run pytest
```

```bash
uv run pytest -m integration tests/integration/ti/test_cenarios_poller.py -s
```

O segundo comando precisa do Postgres de teste local. Ele roda o poller contra
um GLPI e uma OCI falsos, simulando OCI travada, lenta e com erro, GLPI fora do
ar e anexos problemáticos.
