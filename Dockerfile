FROM python:3.11-slim

# Mesma versão do uv usada no desenvolvimento. Instalar pelo `uv.lock`
# (`--frozen`) garante que a imagem tenha EXATAMENTE as versões testadas:
# com `pip install .` cada build pegava a versão mais nova de cada
# biblioteca no dia (achado testando o deploy, 2026-11: a imagem vinha com
# openai 3.52 / pypdf 7.1 enquanto o lockfile e os testes usavam 3.22 / 6.19).
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# oracledb roda em modo thin por padrão (sem Instant Client nativo) — só
# precisa de ORACLE_CLIENT_LIB_DIR se o Oracle exigir Native Network
# Encryption/Data Integrity, o que não é o caso aqui. psycopg[binary] já
# vem com wheel pré-compilada, sem precisar de libpq-dev.
# Dependências primeiro (camada reaproveitada enquanto o lockfile não
# muda), código depois. `--frozen` falha o build se o `uv.lock` estiver
# desatualizado em relação ao `pyproject.toml`, em vez de resolver de novo.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# MCP_HOST=0.0.0.0 é obrigatório aqui — o padrão (127.0.0.1, ver
# .env.example) só aceita conexão de dentro do próprio container, nada de
# fora alcançaria a API. Sobrescrito pelo docker-compose.yml, mantido aqui
# só como fallback caso o container rode isolado sem compose.
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000

EXPOSE 8000

# Processo único de propósito: `server/app.py::main` sobe o poller do GLPI
# como uma task assíncrona em memória (`lifespan_com_poller`) — mais de uma
# réplica ou worker duplicaria esse poller contra o GLPI real. Nunca rodar
# isso com `--workers` ou `deploy.replicas > 1`.
CMD ["agente-oracle"]
