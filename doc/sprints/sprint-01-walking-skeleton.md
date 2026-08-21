# Sprint 01 — Walking skeleton

| | |
| --- | --- |
| Fase | 1.0 — Registro confiável |
| Duração | 2 semanas |
| Estado | planejado |
| Seções da spec | §3, §4, §5, §17, §18, §22.1–22.2 |

## Objetivo

**Uma mensagem de WhatsApp entra, atravessa toda a infraestrutura, e volta uma resposta — sem
nenhum LLM no caminho.**

## Por que este sprint primeiro

A tentação é começar pelo extrator, que é a parte interessante. Seria erro: o extrator é a parte
que a gente **sabe** fazer, e o webhook da Meta é a que pode não funcionar por motivos fora do
nosso controle — verificação de negócio, assinatura HMAC, janela de 24h, latência exigida.

Este sprint troca risco por certeza. Ao final dele, o caminho inteiro está provado e cada agente
subsequente é uma peça encaixada num trilho que já funciona. Se algo na Cloud API for diferente do
que a spec assume — o campo `to` aceitando ou não o `bsuid`, por exemplo (§18.4) —, descobrimos
agora, com dois arquivos de código, e não com dez agentes construídos por cima.

O segundo motivo é custo: **este sprint não gasta um token de LLM.** Toda a fundação é testável de
graça e determinística.

## Escopo

### Dentro

- `docker-compose.yml` com Postgres 16, Redis 7, Qdrant, Caddy e a aplicação
- Schema completo da §5.2 via Alembic: 18 tabelas, constraints, índices, RLS em todas as tabelas
  com `tenant_id`, colunas cifradas já como `BYTEA`
- `settings.py` com pydantic-settings e o `.env.example` do Apêndice A
- `ingress`: `GET`/`POST /webhook/whatsapp`, validação HMAC-SHA256, dedup por `message_id`,
  persistência em `raw_message`, resposta em menos de 200 ms
- Debounce: buffer no Redis, timer de 10s, esvaziamento com `RENAME` atômico (§17.3)
- Worker ARQ com lock FIFO por `bsuid` e `processing_batch` persistido
- Interface `Channel` abstrata + implementação WhatsApp (texto e reação)
- `outbound_queue` com ordenação por `group_id`/`seq` e retry por classe de erro (§18.5)
- Grafo mínimo: `load_context` → `echo` → `voice_stub` → `deliver`
- Criptografia de coluna: helper de cifra/decifra e as colunas da §22.2 em uso
- Suíte de testes com containers efêmeros, incluindo o teste de isolamento entre tenants
- O job `python` do CI saindo de `skipping`

### Fora — e por quê

| Fora | Vai para |
| --- | --- |
| Qualquer agente com LLM | Sprint 02 |
| `LLMGateway`, tiering, fallback | Sprint 02 |
| STT via Groq | Sprint 03 |
| Coleções do Qdrant e RAG | Sprint 04 |
| Catálogo de exercícios semeado | Sprint 02 (o resolver precisa) |
| Golden set e judge | Sprint 02 (não há saída de LLM para avaliar) |
| Langfuse e Datadog | Sprint 02 (o que instrumentar ainda não existe) |
| Billing, proativo, análise | Fases 1.1–1.3 |

O Qdrant sobe no compose neste sprint mesmo sem uso, porque subir infraestrutura é barato agora e
caro no meio de outro sprint.

## Tarefas

Cada uma é uma branch e uma PR, na ordem. As três primeiras podem ser paralelizadas; da quarta em
diante há dependência.

### 1. `feat/project-scaffold`

`pyproject.toml` (Python 3.12, ruff, mypy strict, pytest, asyncpg, arq, fastapi, cryptography),
`settings.py` com pydantic-settings, `.env.example`, `docker-compose.yml`, `Caddyfile`, `Dockerfile`.

**Testes primeiro:** settings carrega do ambiente; settings falha alto quando falta variável
obrigatória; nenhum default silencioso para segredo.

**Pronto quando:** `docker compose up` sobe todos os serviços com healthcheck verde e o job
`python` do CI deixa de ser `skipping`.

### 2. `feat/database-schema`

Alembic e a migração inicial com o schema da §5.2 inteiro.

**Testes primeiro:**

- `alembic upgrade head` numa base vazia não levanta erro — pega `pg_trgm` ausente, expressão não
  imutável em índice, coluna gerada inválida
- `alembic downgrade base` volta ao vazio
- `ck_set_payload` rejeita série `strength` completa sem `reps`, e **aceita** com
  `status='incomplete'`
- `ck_set_payload` rejeita peso externo sem `load_kg` e aceita `is_bodyweight = true` sem carga
- `ux_set_idempotency` bloqueia a segunda inserção do mesmo `source_message_id`, **inclusive quando
  é `NULL`** (é o `NULLS NOT DISTINCT` que importa)
- `ux_tenant_bsuid_active` permite recadastro após `deleted_at` preenchido
- FK composta rejeita `workout_plan` apontando para `training_program` de outro tenant
- **`test_tenant_isolation.py`**: parametrizado sobre a lista de tabelas da §19.1, cada uma
  bloqueia leitura cruzada com `app.tenant_id` definido

**Pronto quando:** os testes passam e o teste de isolamento cobre todas as tabelas da lista.

### 3. `feat/encryption`

Helper AES-256-GCM, `key_version`, e o tipo SQLAlchemy que cifra na escrita e decifra na leitura.

**Testes primeiro:** round-trip preserva o valor; duas cifras do mesmo texto produzem bytes
diferentes (nonce por linha); decifrar com chave errada levanta, não devolve lixo; `key_version`
antigo continua legível após rotação.

**Pronto quando:** as seis colunas da §22.2 são `BYTEA` e passam pelo helper, e nenhum teste lê
plaintext direto do banco.

### 4. `feat/whatsapp-webhook`

`GET /webhook/whatsapp` (verificação), `POST` (recepção), validação HMAC, dedup, `raw_message`,
`conversation_window`.

**Testes primeiro:** assinatura inválida devolve 403 sem tocar o banco; assinatura válida devolve
200; comparação de assinatura é em tempo constante; `message_id` repetido não insere duas vezes;
o handler responde **antes** de qualquer trabalho pesado; primeiro contato faz upsert do `tenant`
antes de gravar `raw_message` (§5.2).

**Pronto quando:** p99 do endpoint abaixo de 200 ms em teste de carga local.

### 5. `feat/burst-debounce`

Buffer, timer de 10s renovável e esvaziamento atômico.

**Testes primeiro:**

- Quatro mensagens em 7s produzem **um** lote de quatro
- Mensagem chegando entre o `RENAME` e o `DEL` **não** se perde — o teste que prova por que
  `LRANGE`+`DEL` estava errado (§17.3)
- Buffer vazio no flush não cria lote
- Chave `drain:` órfã é recolhida pelo job de manutenção

### 6. `feat/worker-queue`

Worker ARQ, lock por `bsuid` com auto-extensão, `processing_batch`, retry com backoff.

**Testes primeiro:** duas rajadas do mesmo usuário serializam; rajadas de usuários diferentes
correm em paralelo; lock expirado não permite processamento concorrente; job que falha reenfileira
com o batch persistido; após 3 tentativas marca `failed` e produz mensagem de degradação.

### 7. `feat/outbound-delivery`

Interface `Channel`, cliente WhatsApp, `outbound_queue` com ordenação e retry por classe de erro.

**Testes primeiro:**

- Bolha `seq=1` não sai antes da `seq=0` ter `sent_at`
- Worker reiniciado no meio de um grupo retoma do ponto certo, sem reenviar prefixo
- `131047` não repete e converte para template
- `131026` marca `undeliverable` e não repete
- `130429` repete com backoff e jitter
- `100` não repete e alerta
- Bolha `dead` marca as seguintes do grupo como `dead`

### 8. `feat/echo-graph`

`GraphState` tipado com os reducers da §8.1, checkpointer Postgres, e o grafo mínimo
`load_context → echo → voice_stub → deliver`.

**Testes primeiro:** estado sobrevive entre invocações na mesma `thread_id`; dois nós escrevendo
`outbound` em paralelo não levantam `InvalidUpdateError` (o teste que prova o reducer); poda mantém
no máximo 12 mensagens.

**Pronto quando:** mandar "oi" pelo WhatsApp devolve uma reação ✅.

## Critérios de saída

Verificáveis, na ordem em que devem ser conferidos:

| # | Critério | Como verificar |
| --- | --- | --- |
| 1 | Infra sobe do zero | `docker compose up` com todos os healthchecks verdes |
| 2 | Schema aplica limpo | `alembic upgrade head` numa base vazia, sem erro |
| 3 | Migração é reversível | `alembic downgrade base` volta ao vazio |
| 4 | Ida e volta real | Mandar "oi" de um celular e receber ✅ em menos de 5s |
| 5 | Rajada agrupa | 4 mensagens em 10s geram 1 `processing_batch` |
| 6 | Idempotência | Reentrega do mesmo `message_id` não duplica `raw_message` |
| 7 | Isolamento | `pytest tests/test_tenant_isolation.py` verde para todas as tabelas |
| 8 | Ordem durável | Matar o worker no meio de um split não reenvia prefixo nem perde sufixo |
| 9 | Latência do webhook | p99 < 200 ms sob carga local |
| 10 | CI real | Job `python` roda e passa, não mais `skipping` |
| 11 | Nada em claro | `SELECT verbatim FROM health_report` devolve bytes, não texto |

## Riscos

| Risco | Impacto | Plano |
| --- | --- | --- |
| Verificação de negócio da Meta demora | **Bloqueia o critério 4** | Começar a verificação no dia 1. Enquanto não sai, testar com o número de teste da Cloud API, que não exige verificação |
| `to` não aceitar `bsuid` (§18.4) | Alto — muda o schema do `tenant` | É o primeiro teste do critério 4. Se falhar, `tenant` ganha coluna de endereço separada da identidade e abre-se um ADR |
| RLS com `FORCE` quebrar as migrações | Médio | Migração roda com role dona e `SET LOCAL row_security = off`; o teste 7 cobre |
| Coluna cifrada quebrar índice ou constraint existente | Médio | O teste 2 pega na primeira execução |
| `NULLS NOT DISTINCT` exigir PG15+ | Baixo | Compose fixa `postgres:16-alpine`; o teste 2 falharia em versão menor |

## O que este sprint deliberadamente não prova

Que a extração funciona, que o resolver acerta, que a persona agrada. Nada disso tem dado ainda —
e é exatamente por isso que o sprint 02 começa com o `LLMGateway` e o golden set: para que a
primeira saída de LLM já nasça medida.
