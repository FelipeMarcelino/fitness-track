# Sprint 03 — LangGraph Core, Ingestion Agents and the Output Path

| Campo | Valor |
| --- | --- |
| Fase | 1.0 — Registro confiável |
| Duração | 2 semanas |
| Estado | `planned` |
| Objetivo | Fechar o ciclo completo: a rajada persistida pela Sprint 02 atravessa o grafo LangGraph, vira série gravada, e a resposta chega ao usuário |
| Referências principais | spec §§4, 5.3, 6, 7, 8, 9, 10, 13, 17, 18.4, 19.1, 20.2, 21.4, 22.3, 23, 24 |
| Issues incluídas | [#29](https://github.com/FelipeMarcelino/fitness-track/issues/29), [#30](https://github.com/FelipeMarcelino/fitness-track/issues/30) |

> **Escopo fechado em 2026-09-04.** O planejamento foi executado em quatro fatias paralelas e produziu
> 28 tarefas; o operador cortou para **22**. As 26 ambiguidades levantadas foram todas decididas — o
> registro está em [Decisões tomadas](#decisões-tomadas), e a seção
> [Ambiguidades](#ambiguidades--registro-e-leitura-recomendada) preserva o raciocínio de cada uma.
>
> **Seis ADRs precisam existir antes da primeira PR de código** (ADR-0010 a ADR-0015). Estão na onda 0
> da [Ordem de PRs](#ordem-de-prs).

## Resultado esperado

Ao final da sprint, a frase `"Supino reto com 10 kg, 8 repetições e foi fácil"` percorre o caminho
inteiro sem intervenção: o webhook da Sprint 02 persiste e bufferiza, o worker drena o lote, o grafo
LangGraph normaliza, roteia, extrai, resolve o exercício, abre a sessão, grava as séries, o
`voice_agent` decide a resposta, o `deliver` enfileira e o drain entrega no Telegram.

Três consequências operacionais fecham junto:

- a `outbound_queue` deixa de ser um cemitério — hoje nada a drena, e toda resposta persistida pelo
  caminho de voz da Sprint 02 é silêncio para o usuário (**issue #29**);
- a reentrega de um evento de bloqueio deixa de cunhar tenant órfão (**issue #30**);
- o ADR-0009, que autorizou respostas fixas fora do grafo como antecipação **com prazo de
  expiração nesta sprint**, é resolvido — aposentado ou reescrito, nunca deixado a vencer em
  silêncio.

Esta sprint **não** implementa análise de evolução (fase 1.1) nem recomendação de fichas
(fase 1.2). Os nós `analysis` e `recommendation` existem na topologia como degradação honesta.

## Escopo

Incluído:

- `GraphState`, reducers e a regra de estagiamento da §8.8, com `test_graph_reducers` e
  `test_graph_topology` — os dois testes de arquitetura que o `CLAUDE.md` promete para "a mesma PR
  que introduzir o que eles verificam";
- grafo raiz da §8.3 completo, subgrafos com schema privado e contrato de falha parcial da §8.9;
- `AsyncPostgresSaver`/`AsyncPostgresStore` ligados, com grants pós-`setup()`, serializer cifrado,
  namespace injetado por tenant, principal exclusivo, thread por tenant e poda de checkpoints;
- `process_batch` invocando o grafo — o handoff que a Sprint 02 deixou marcado em comentário;
- `LLMGateway` com resolução por papel, retry, fallback, quota e `usage_ledger`;
- agentes da ingestão: normalizer, extraction + mapa de RPE, resolver de exercícios;
- catálogo global de exercícios e a coleção `exercise_catalog` no Qdrant;
- sessão de treino: abertura, reabertura e persistência de séries com expansão de `repeat`;
- agentes de decisão e saída: `router`, `guardrail`, `voice_agent`;
- onboarding guiado com perfil mínimo e consentimentos LGPD versionados — sem ele o tenant nasce em
  `onboarding` e nunca sai;
- caminho de saída completo: nó `deliver`, função de claim, drain job, retry e dead-letter;
- issues #29 e #30 — a #29 fechada **inteiramente pelo caminho do grafo** (T16 + T19 + T28), sem
  enqueue no ingress.

Adiado para a Sprint 04 pela decisão de escopo (as tarefas continuam escritas neste documento):

- **T13** clarification e registro `incomplete`;
- **T14** manutenção de ciclo de vida da sessão;
- **T20** lista de redação PII da §20.2 verificada por teste — ver o risco declarado em
  [Riscos](#riscos-e-mitigação);
- **T24** `correction_agent` (depende do ADR-0017);
- **T25** `summary_agent`.

A **T17** foi cortada, não adiada: a T28 fecha o mesmo comportamento pelo caminho certo.

Fora do escopo:

- `analysis_agent`, `ToolNode`, `narrator`, `numeric_critic` e as 11 tools SQL da §16 — **fase 1.1**;
- `recommendation_agent`, `plan_validator`, `program_agent`, `program_validator`, `context_builder`,
  `rag_retriever` — **fase 1.2**;
- RAG de conversas, literatura e templates (§15) — exceção única é a coleção `exercise_catalog`
  exigida pelo §10;
- coach proativo, gamification, gráficos e progressão — fases 1.1/1.2;
- instrumentação Langfuse/Datadog completa (exporter, DSN, dashboards, alertas da §20.5) — esta
  sprint entrega o *seam* (`astream(stream_mode=["updates","custom"])`) e a lista de redação
  testável, não o backend;
- promoção global de aliases e dedup semanal de exercícios privados — atravessam tenants e não têm
  fronteira RLS autorizada (ver [fronteira de manutenção](#a-fronteira-de-manutenção-cross-tenant));
- golden set v1 de 150 casos — mede agentes que só existem no fim desta sprint;
- object storage para mídia na fila — a condição de reabertura do ADR-0005 não ocorreu;
- WhatsApp: adaptador, janela de 24h, templates, `statuses`, vínculo entre canais — **fase 2.0**;
- paralelismo *medido* (o ganho de 32% da §8.8) — a mecânica entra agora e é testada com ramos
  sintéticos; o ganho só existe quando `analysis` e `recommendation` forem reais.

## Princípios de execução

1. **Ninguém enfileira exceto `deliver`; ninguém decide exceto `voice_agent`.** A invariante 2 é o
   eixo desta sprint: ela nasce aqui, e é aqui que o ADR-0009 tem de ser resolvido.
2. **`tenant_id` nunca vem do LLM.** `thread_id = f"tenant:{tenant_id}"` é construído em um único
   lugar, a partir do código, e um teste afirma que é o único construtor no repositório.
3. **Todo ID devolvido por um LLM é validado contra um conjunto que o código já autorizou.** O
   resolver só aceita `exercise_id` que esteja entre os cinco candidatos já filtrados por tenant.
4. **Toda entrada externa é dado.** `clean_text`, transcrição, nome privado de exercício e chunk
   recuperado continuam delimitados e não confiáveis a jusante (invariante 7, §22.3).
5. **Onde há gabarito, código.** Nenhum crítico LLM-julgando-LLM. Nesta sprint os controles são
   schema Pydantic, resolver determinístico, `CHECK` do banco e `status='incomplete'`.
6. **RLS é a fronteira, e ela morde.** Qualquer varredura que atravesse tenants passa por função
   `SECURITY DEFINER` estreita — nunca pelo DSN de owner, nunca por privilégio ampliado no runtime.
7. **Nenhum comportamento começa sem teste que falhe pelo motivo esperado.** Testes unitários usam
   providers falsos; Postgres e Qdrant só na integração; relógio injetado, nunca `sleep`.

## Tarefas

A numeração é **estável por trilha**, não por ordem de execução — a ordem real está em
[Ordem de PRs](#ordem-de-prs). Trilha A = núcleo do grafo (T01–T05), trilha B = agentes da ingestão
(T06–T14), trilha C = saída e entrega (T15–T20), trilha D = agentes de decisão e saída (T21–T28).

### Trilha A — Núcleo do grafo

#### S03-T01 — Graph state and staging rule

**Objetivo.** Declarar `GraphState`, os tipos de plano e a função determinística `stage_plan()`, com
o teste de arquitetura que sustenta a §8.8.

**Spec.** §8.2, §8.8, §9.4 regra 1, §21.4, §23. Invariante 9.

**Depende de:** nada. É a raiz de tudo — A, B e C dependem dela.

**Arquivos previstos:** criar `src/fittrack/graph/state.py`, `src/fittrack/graph/staging.py`,
`tests/test_graph_reducers.py`, `tests/unit/test_staging.py`; modificar
`tests/test_channel_isolation.py` e o `Makefile`.

**Plano de implementação:**

1. `state.py` com `Target`, `RouteStep`, `PlanStage` e a base do `GraphState` da §8.2, mais as erratas
   obrigatórias desta sprint: `destination_identity_id` na entrada (o `identity_id` da última mensagem
   da rajada, nunca inferido de `reply_to`) e `voice_output` na T21. As quatro listas concorrentes
   (`extracted_sets`, `persisted_set_ids`, `outbound`, `errors`) usam `resettable_add`: para updates
   normais ele delega a `operator.add`; um único `BatchReset([])` emitido pelo runner antes de um
   **batch novo** substitui o valor acumulado. Uma lista vazia comum não reseta nada. O pin 0.6 não
   oferece `Overwrite`, portanto o sentinel é nosso, fechado e testado — não um `dict` que o LLM possa
   fabricar. `messages` usa `bounded_add_messages`, que preserva a semântica de `add_messages` e poda
   deterministicamente para as 12 últimas. `analysis_result`/`recommendation`/`query_result`/
   `health_flag` continuam **sem** reducer porque têm escritor único e são sobrescritas a cada batch.
2. Declarar no mesmo módulo `CONCURRENT_KEYS` e `SINGLE_WRITER_KEYS` (com o ramo dono anotado). Uma
   chave nova que não apareça em nenhum dos dois **reprova o teste** — é o que impede o campo
   acrescentado sem decisão.
3. `stage_plan(steps) -> list[PlanStage]` com os três casos da §8.8 (`ingestion` sozinho no estágio
   1; o resto no estágio 2; sem `ingestion`, estágio único) e o teto da §9.4 regra 4: 4 passos por
   estágio, truncando com registro em `errors`.
4. **Resolver o conflito com `test_channel_isolation`** — ver
   [ambiguidade #3](#3-channel_caps-no-graphstate-reprova-o-teste-de-isolamento-hoje). Separar
   `IMPORT_EXCEPTIONS` de `CAPS_EXCEPTIONS`, e acrescentar a asserção substituta: `state.py` menciona
   `channel_caps` exatamente uma vez, e apenas como anotação de campo.
5. O teste do reducer cobre três transições distintas: fan-out do mesmo batch concatena; entrada vazia
   não apaga; `BatchReset([])` vindo do runner apaga exatamente uma vez antes do batch seguinte.
   `bounded_add_messages` é exercitado com 13+ mensagens e nunca devolve mais de 12.

**Primeiro teste que deve falhar.**
`tests/test_graph_reducers.py::test_a_parallel_stage_merges_every_concurrent_key` →
`ModuleNotFoundError: No module named 'fittrack.graph.state'`. Depois do módulo existir, monta um
`StateGraph` sintético com um `dispatch` devolvendo quatro `Send`, cada nó retornando todas as
chaves de `CONCURRENT_KEYS`, e afirma `len(result["outbound"]) == 4` e `len(result["errors"]) == 4`.
Remover o reducer (ou trocar `resettable_add` por overwrite simples) faz o LangGraph levantar
`InvalidUpdateError`; o teste separado de reset prova que o reducer ainda concatena updates normais.

**Critérios de aceite:** `pytest tests/test_graph_reducers.py tests/unit/test_staging.py -v` verde;
`make test-architecture` roda **dois** arquivos; `mypy --strict` verde sobre `GraphState`; um campo
novo sem classificação reprova `test_every_state_key_is_classified` (demonstrado no PR com commit
temporário revertido); duas invocações no mesmo `thread_id` provam que as quatro listas do segundo
batch não carregam nenhum item do primeiro, enquanto `messages`/`conversation_digest` atravessam a
fronteira e `messages` permanece limitada a 12.

**Tamanho:** M.

#### S03-T02 — Root graph topology

**Objetivo.** Montar o grafo raiz da §8.3 com todos os nós declarados — reais onde determinísticos,
*stubs* onde as trilhas B e D preenchem — e entregar `test_graph_topology.py`.

**Spec.** §8.3, §8.4, §9.4, §13.5, invariante 2, §21.4, §23.

**Depende de:** S03-T01.

**Arquivos previstos:** criar `src/fittrack/graph/root.py`, `graph/nodes/dispatch.py`,
`graph/nodes/join.py`, `graph/nodes/load_context.py`, e *stubs* de `normalizer.py`, `guardrail.py`,
`router.py`, `voice.py`, `deliver.py`; criar `tests/test_graph_topology.py`; modificar `Makefile`.

**Plano de implementação:**

1. `build_root_graph(nodes: NodeBundle, *, saver=None, store=None)` — a topologia é montada por uma
   fábrica que **recebe os callables dos nós**. É o que permite esta tarefa fechar sem esperar B e D:
   o `NodeBundle` tem defaults determinísticos, e as outras trilhas trocam cada campo sem tocar em
   uma aresta.
2. Arestas exatamente como a §8.3. `add_conditional_edges("dispatch", dispatch, [os cinco alvos])`
   **com o terceiro argumento obrigatório** — é o que dá erro cedo num alvo inexistente (§8.4).
   `add_node("join", advance_stage, defer=True)`. `voice → deliver → END`.
3. `RetryPolicy(max_attempts=2, retry_on=(TransientLLMError,))` nos nós de LLM e
   `RetryPolicy(max_attempts=3)` no `deliver`. `TransientLLMError` vem de `fittrack/llm/errors.py`
   (trilha B) — **dependência cruzada bloqueante, acordar antes do merge**.
4. `recursion_limit` **não** vai no compile: é config de invocação (T05).
5. `analysis` e `recommendation` entram como nós de degradação — ver
   [ambiguidade #14](#14-os-cinco-alvos-do-router-existem-na-fase-10).

**Primeiro teste que deve falhar.** `tests/test_graph_topology.py::test_only_deliver_reaches_end` →
`ImportError: cannot import name 'build_root_graph'`. Depois:
`assert {e.source for e in drawable.edges if e.target == END} == {"deliver"}`.

**O que `test_graph_topology.py` verifica:** todo nó alcançável a partir de `START`; todo nó alcança
`END`; único predecessor de `END` é `deliver` e único predecessor de `deliver` é `voice`;
`set(get_args(Target))` igual aos alvos do dispatch; todo destino declarado em `Command[Literal[...]]`
existe como nó (**a armadilha que a §8.4 nomeia** — por isso o teste lê a anotação, não o diagrama);
`join` declarado com `defer=True`; `max_attempts` correto por nó.

**Critérios de aceite:** `make test-architecture` roda os **três** arquivos; o `draw_mermaid()` com os
12 nós da §8.3 é colado no corpo do PR; remover `"voice"` do `Literal` do `guardrail` reprova o
item 5 (demonstrado e revertido); `mypy --strict` verde.

**Tamanho:** M/G.

#### S03-T03 — Subgraph shells and partial-failure contract

**Objetivo.** Os cinco subgrafos como `StateGraph` compilados com schema privado, mais o contrato de
falha parcial da §8.9 — um ramo que explode não derruba o estágio.

**Spec.** §8.4, §8.5, §8.6, §8.9, §9.4.

**Depende de:** S03-T02.

**Arquivos previstos:** criar `graph/subgraphs/{ingestion,analysis,recommendation,admin}.py`,
`graph/nodes/smalltalk.py`, `graph/failure.py`, `tests/unit/test_subgraph_isolation.py`.

**Plano de implementação:**

1. Cada subgrafo declara um `TypedDict` privado que estende `GraphState` com suas chaves internas
   (`resolver_candidates`, `tool_rounds`, `validator_feedback`) como `input_schema`; o `output_schema`
   é o `GraphState` puro. O que não está no `output_schema` não vaza para o pai nem entra no
   checkpoint.
2. **`current_step` mora no schema privado**, não no `GraphState` — ver
   [ambiguidade #4](#4-current_step-não-existe-no-graphstate-da-82).
3. Nenhum subgrafo recebe `checkpointer=` (§8.4: herda o do pai; passar quebra o `interrupt` da §8.7).
   Um teste afirma isso.
4. `failure.py`: wrapper que converte a exceção em `{"errors": [f"{target}: {tipo}"]}` e **nunca**
   relança. O `except` é largo aqui e estreito nos níveis de cima, conforme a hierarquia da §8.9 —
   mas registra tipo e traceback em log estruturado para não engolir bug de programação.
5. A ingestão entra com a topologia da §8.5 desenhada e os nós como *stub* (trilha B preenche),
   incluindo as arestas condicionais e o desvio `is_correction=True → Command(goto="correction")`.

**Primeiro teste que deve falhar.**
`test_a_failing_branch_records_an_error_and_does_not_raise` →
`AttributeError: module ... has no attribute 'build_ingestion_graph'`. Depois:
`assert result["errors"] == ["ingestion: RuntimeError"]` e
`assert [b["kind"] for b in result["outbound"]] == ["smalltalk"]`.

**Critérios de aceite:** ramo que explode deixa o outro ramo do mesmo estágio intacto;
`test_private_keys_do_not_leak_to_the_parent` verde;
`test_no_subgraph_declares_its_own_checkpointer` verde; `make test-architecture` continua verde com
5 módulos de subgrafo reais sob vigilância.

**Tamanho:** M.

#### S03-T04 — Checkpointer, store and thread identity

**Objetivo.** Ligar `AsyncPostgresSaver` e `AsyncPostgresStore` por uma fronteira cifrada e escopada
por tenant, conceder os privilégios somente depois que `setup()` criar as tabelas, fixar `thread_id`
e podar checkpoints.

**Spec.** §5.3, §8.4, §8.7, §19.1.

**Depende de:** S03-T02.

**Arquivos previstos:** modificar `graph/root.py`, `worker.py`, `settings.py`, `security/crypto.py`,
`scripts/bootstrap.py`, `.env.example`, `docker-compose.yml`, `docker-compose.dev.yml`,
`docker/postgres/initdb/00-roles.sh`, `scripts/init_dev_env.py`; criar `graph/persistence.py`,
`graph/store.py`,
`doc/adr/0010-fronteira-de-tenant-nas-tabelas-do-langgraph.md`,
`tests/unit/test_tenant_store.py`, `tests/integration/test_graph_checkpoint.py`. **Não existe migração
de grant nas tabelas do LangGraph:** Alembic roda antes de `setup()` num banco limpo e não pode dar
`GRANT` em relações que ainda não existem.

**Plano de implementação:**

1. `thread_id_for(tenant_id) -> f"tenant:{tenant_id}"` num único lugar, **nunca derivado do estado
   ou do LLM** (invariante 3). Teste por AST afirma que é a única construção no repositório.
2. Um principal de login exclusivo, `fittrack_graph`, recebe `LANGGRAPH_DATABASE_URL` próprio, não é
   membro de `fittrack_app`, é `NOSUPERUSER NOBYPASSRLS` e não recebe privilégio em tabela de domínio.
   O saver/store nunca usam `DATABASE_URL` nem `MIGRATION_DATABASE_URL`. psycopg fala libpq, então a
   URL perde apenas o sufixo `+asyncpg` por função compartilhada. O pool usa
   `kwargs={"autocommit": True, "row_factory": dict_row}`.
3. A ordem idempotente do bootstrap fica explícita e coberta por integração: **Alembic →
   `saver.setup()`/`store.setup()` como owner → revogar o DML herdado por `fittrack_app` →
   `configure_langgraph_grants()` conceder ao `fittrack_graph` o DML mínimo nas quatro relações
   operacionais** (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `store`); as duas tabelas de
   migração interna permanecem exclusivas do owner, pois o runtime nunca chama `setup()`. A função
   pós-`setup()` valida as seis relações realmente descobertas e falha alto se a lista esperada e a
   lista criada divergirem. Rodar de novo refaz revoke/grant sem erro; nenhuma migração referencia
   essas relações.
4. `PersistenceFactory.for_tenant(tenant_id)` constrói, sobre pools compartilhados, um
   `TenantEncryptedSaver`: adaptador da porta pública de checkpointer que envolve **todo** valor de
   canal — inclusive `str`/`int`/`bool`, que o PostgresSaver 2.0.25 embute no JSONB e não entrega ao
   serializer — num envelope não primitivo antes de delegar. O delegate usa `EncryptedSerializer` do
   próprio `langgraph-checkpoint` e `TenantCheckpointCipher` ligado a `thread_id_for(tenant_id)`; na
   leitura o adaptador decifra e remove o envelope antes de o grafo ver o valor. O AAD inclui o
   tenant/thread derivados pelo código; ler o blob com a fronteira de outro tenant falha autenticação.
   `checkpoint`/`metadata` ficam restritos a versões, ids e uma allowlist de metadados sem conteúdo. A
   fábrica compila a instância do grafo para aquela invocação, sem cache mutável de processo, e nunca
   chama `setup()` no runtime.
5. O `AsyncPostgresStore` bruto nunca é entregue a nó ou `Runtime`. `TenantScopedStore` implementa a
   porta do LangGraph e prefixa **toda** operação `get/search/put/delete` com
   `("tenant", str(tenant_id), "profile")`; o namespace fornecido pelo chamador é sempre sufixo e não
   pode substituir nem escapar do prefixo. O wrapper também cifra o valor antes do JSONB com AAD
   canônico de tenant + namespace + key e só ele decifra na leitura.
6. Testes com dois tenants usam a mesma `key`, tentam deliberadamente um namespace com prefixo alheio
   e provam resultados disjuntos. Uma consulta feita como owner confirma que o literal do usuário não
   aparece em `checkpoint_blobs.blob`, `checkpoint_writes.blob` nem `store.value`; trocar os blobs
   entre tenants falha autenticação. O teste procura o literal também no JSONB de
   `checkpoints.checkpoint`/`metadata` e usa um `conversation_digest` escalar para provar que o
   adaptador não deixa o atalho de primitivos furar o serializer. O teste inspeciona o banco bruto —
   round-trip pela API sozinho não prova criptografia.
7. Retenção (§5.3, "não é opcional"): cron ARQ diário no padrão do `sweep_voice_audio` já existente,
   apagando checkpoints antigos **exceto o último de cada thread** — é ele que carrega um `interrupt`
   pendente. Toda a fronteira acima fica registrada no **ADR-0010**.

**Primeiro teste que deve falhar.**
`test_the_graph_principal_can_write_only_after_post_setup_grants` →
`InvalidAuthorizationSpecification: role "fittrack_graph" does not exist` (ou `permission denied`)
contra o banco saído do `make bootstrap`. O teste executa o caminho de banco limpo; inverter a ordem e
voltar a aplicar grant por Alembic produz `UndefinedTable` antes de `setup()` e reprova explicitamente.

**Critérios de aceite:** `make bootstrap` duas vezes e a partir de banco vazio continua idempotente;
`test_alembic_did_not_create_or_grant_the_langgraph_tables` verde; `fittrack_graph` grava/retoma estado
cifrado (inclusive primitivos), não lê tabela de domínio e `fittrack_runtime` continua sem acesso
direto às tabelas do LangGraph; namespace forjado não cruza tenant; literal sensível não aparece em
nenhuma coluna do banco bruto; poda deixa exatamente 1 checkpoint por thread.

**Tamanho:** G. Se alongar, dividir em T04a (bootstrap pós-`setup()` + ADR + principal) e T04b
(serializer + store escopado + retenção) — T04b depende da fronteira criada pela primeira metade.

#### S03-T05 — Worker integration: `process_batch` invokes the graph

**Objetivo.** Fechar o handoff da Sprint 02. `process_batch` decifra o lote, monta o estado inicial,
invoca o grafo com o thread e o `recursion_limit` corretos, e trata retomada de `interrupt` e
degradação.

**Spec.** §4, §4.1, §4.2, §8.4, §8.7, §17.1, §9.4 regra 3.

**Depende de:** S03-T02, S03-T03, S03-T04, S03-T08 e S03-T26. **É a última da trilha A e cola as
trilhas B, C e D no worker real.**

**Arquivos previstos:** modificar `services/batch.py`, `graph/nodes/load_context.py`, `worker.py`;
criar `graph/runner.py`, `services/conversation_history.py`,
`tests/integration/test_graph_handoff.py`, `tests/integration/test_conversation_state.py`,
`tests/unit/test_initial_state.py`; modificar `doc/sprints/02-telegram-pipeline.md` (relatório de
encerramento: handoff cumprido).

**Plano de implementação:**

1. Decifrar `combined_text` com o AAD correto e mapear para `raw_fragments`, **descartando
   `external_id_hash` e `media_ref`** — o primeiro por invariante 10, o segundo porque é referência
   de acesso reutilizável (§20.6) e nada a jusante o lê. `raw_message_id` permanece como referência
   interna auditável; o serializer tenant-bound da T04 garante que texto e turno nunca chegam em
   claro às tabelas de checkpoint.
2. Sob o lock do tenant, resolver o último `raw_message_id` do batch por query tenant-scoped e carregar
   juntos `identity_id`, `channel` e `channel_message_id`. Esses valores originam
   `destination_identity_id`, `origin_channel` e `reply_to`; se a linha não existir ou não pertencer ao
   tenant, o batch falha antes do grafo. Nunca recuperar a identidade a partir de `reply_to`: dois
   chats podem ter o mesmo `channel_message_id` e um tenant pode ter várias identidades.
3. **Quem monta `channel_caps` é o worker, não o grafo.** A §4 põe o carregamento de capacidades
   antes do `ainvoke`; a §9.2 também lista um nó `load_context`. A leitura que concilia as duas com o
   AD-39: o **worker** (fora de `graph/`, logo autorizado a importar `channels/`) resolve
   `origin_channel`, `reply_to` e `channel_caps`; o **nó `load_context`** carrega o que é domínio —
   `profile`, `active_session`, `now_local`, quota. É isso que mantém `load_context.py` sem uma
   menção a `channel_caps`.
4. Antes de uma invocação normal, o runner lê o último checkpoint ainda sob o mesmo lock. Se o
   `batch_id` mudou, envia `BatchReset([])` para `extracted_sets`, `persisted_set_ids`, `outbound` e
   `errors`, e sobrescreve os campos de escritor único com os defaults do batch; se é retry do mesmo
   `batch_id` ou `Command(resume=...)`, **não reseta** e retoma. Passar quatro listas vazias comuns é
   proibido porque o reducer apenas as concatena e não apagaria o batch anterior.
5. `messages` atravessa batches pelo mesmo thread, mas `bounded_add_messages` da T01 nunca deixa mais
   de 12. A cada 20 interações concluídas, `ConversationHistoryService` carrega as 20 referências
   canônicas de entrada/saída das tabelas de domínio, decifra só em memória, chama o papel `SUMMARY`
   e faz `aupdate_state` de `conversation_digest` antes de `mark_done`; um watermark cifrado no
   `TenantScopedStore` torna a atualização idempotente. Assim as oito mensagens que saem da janela
   curta não precisam permanecer em claro nem crescer no checkpoint para alimentar o resumo.
6. `astream(stream_mode=["updates","custom"])` em vez de `ainvoke` — é o que a §8.4 pede e o que a
   §20.1 consome depois. Nesta sprint o consumidor de `updates` é só o log estruturado.
7. `durability` síncrono (§8.7) — **verificar a grafia contra a versão fixada** e testar o
   comportamento (checkpoint legível entre super-steps), nunca o nome do kwarg. Ver
   [risco #5](#riscos-e-mitigação).
8. `interrupt`: gravar `interrupt:{tenant_id}` com TTL de 20 min (§17.1); no lote seguinte, se
   `turn.answers_clarification`, retomar por `Command(resume=...)`. A decisão é do normalizer
   (trilha B); a trilha A entrega a **máquina**. Ver
   [ambiguidade #6](#6-um-interrupt-expirado-no-redis-não-é-varrível).
9. `GraphRecursionError` e `QuotaExceeded` → degradação graciosa + alerta, nunca silêncio.
10. `mark_done` só depois do `astream` e da manutenção do digest completarem; falha vira `Retry` do
   ARQ, e o checkpointer garante
   que o retry não repete o trabalho já feito (§4.1).

**Primeiro teste que deve falhar.**
`test_process_batch_invokes_the_graph_with_the_tenant_thread` →
`TypeError: process_batch() got an unexpected keyword argument 'runner'`. Depois, com um spy:
`assert spy.config["configurable"]["thread_id"] == f"tenant:{tenant_id}"`,
`assert spy.config["recursion_limit"] == 40`,
`assert spy.state["destination_identity_id"] == last_raw.identity_id` e
`assert "external_id_hash" not in spy.state["raw_fragments"][0]`.

**Critérios de aceite:** batch fica `done` só depois de o grafo terminar; matar o processo no meio do
`astream` e reprocessar o mesmo `batch_id` **retoma do último super-step**; nenhum `external_id`,
`external_id_hash`, `media_ref` ou `file_path` aparece no estado **serializado no checkpoint** (não
só no log), e nenhum texto do usuário aparece no banco bruto; dois batches consecutivos no mesmo
thread não compartilham acumuladores, retry do mesmo batch não os apaga, 25 interações deixam no
máximo 12 mensagens e atualizam o digest exatamente uma vez no marco 20; duas identidades com o mesmo
`channel_message_id` entregam para a identidade do último `raw_message_id`; o lock por tenant cobre o
`astream` inteiro e o auto-extend de 30s sobrevive a um grafo de 60s.

**Tamanho:** G. Não dá para encolher: `ainvoke` sem construção de estado é intestável, e construção de
estado sem `ainvoke` não prova nada.

### Trilha B — Agentes da ingestão

#### S03-T06 — LLM gateway role dispatch

**Objetivo.** O contrato único de invocação de LLM. `ainvoke` exige `agent`, `role`, `tenant_id`,
mensagens e schema opcional; resolve modelo exclusivamente por `ModelsConfig.resolve(agent, role)`.

**Spec.** §7.1, §7.2, §7.2.1, §7.4; AD-19/ADR-0001, AD-43; invariantes 3, 4 e 7.

**Depende de:** Sprint 02. **Independente da trilha A** — pode começar no dia 1, em paralelo com T01.

**Já existe:** `LLMRole`, `AGENT_ROLES` e `ModelsConfig.resolve()` em `llm/roles.py`, e os dez papéis
em `config/models.yaml`. Falta o gateway, os providers e os agentes.

**Arquivos previstos:** criar `llm/gateway.py`, `llm/types.py`, `llm/providers/{base,groq,anthropic}.py`,
`tests/unit/test_llm_gateway.py`, `tests/unit/test_llm_provider_params.py`; modificar `llm/__init__.py`.

**Plano de implementação:** portas injetáveis de provider e `LLMResult` (testes usam fakes, sem rede);
**validar toda resposta com `schema.model_validate()` mesmo quando o provider promete structured
output** (a validação é a fonte da verdade, não a promessa); normalizar parâmetros por
`(provider, model)` — não enviar `temperature` à Anthropic nem `reasoning_format` ao `gpt-oss`;
nenhum agente instancia SDK de provider.

**Primeiro teste que deve falhar.** `test_ainvoke_resolves_model_by_agent_and_role` com
`assert fake.calls[0].model == models.resolve(agent="extraction", role=LLMRole.EXTRACTOR).primary.model`.

**Critérios de aceite:** omitir `agent` ou `role` falha; resposta com campo extra ou enum inválido
falha em Pydantic; **nenhum identificador de modelo aparece em Python** —
`test_no_model_name_appears_in_python` permanece verde (invariante 4).

**Tamanho:** M. **Bloqueado pela [ambiguidade #1](#1-sdk-nativo-vs-langchain-na-camada-de-provider).**

#### S03-T07 — LLM fallback, quota and accounting

**Objetivo.** Retry, fallback, recarga de `models.yaml`, quota pré-chamada e escrita em
`usage_ledger`, sem logar prompts, respostas ou secrets.

**Spec.** §7.1, §7.3, §7.4, §19.3, §20.1, §20.3; ADR-0004; invariantes 4, 5, 7 e 10.

**Depende de:** S03-T06.

**Arquivos previstos:** criar `llm/config_cache.py`, `llm/cost.py`, `repositories/usage.py`,
`services/quota.py`, `tests/unit/test_llm_fallback.py`, `tests/unit/test_llm_config_reload.py`,
`tests/integration/test_llm_usage_ledger.py`; modificar `llm/gateway.py`, `startup.py`, `worker.py`,
`docker-compose.yml`, `.env.example`.

**Plano de implementação:** aplicar exatamente a política da §7.3 (duas tentativas no primário para
429/5xx/timeout/conexão; fallback depois; 400 comum **não** faz fallback, limite de contexto **faz**);
em falha de schema, uma correção e depois fallback, com `was_fallback` chegando ao ledger; recarregar
`models.yaml` após 60s ou SIGHUP validando antes de trocar o snapshot; **exigir no worker apenas as
credenciais dos providers que ele pode chamar** — o ingress não recebe credencial de LLM por
conveniência.

**Primeiro teste que deve falhar.** `test_gateway_retries_primary_twice_then_records_fallback` com
`assert primary.calls == 2 and fallback.calls == 1 and ledger.rows[0].was_fallback is True`.

**Critérios de aceite:** `make test-integration` prova a linha correta em `usage_ledger`; exceder
quota **não chama provider**; captura de logs prova ausência de mensagens, transcrições, payloads,
tokens e nomes privados.

**Tamanho:** M.

#### S03-T08 — Conversation normalizer contract

**Objetivo.** A única entrada de domínio: rajada → `NormalizedTurn` limpo, segmentado, rotulado e
auditável. Single-shot, papel `NORMALIZER`, **sem** extração de séries.

**Spec.** §8.3, §9.2, §9.3, §12.3, §22.3; AD-17, AD-42; ADR-0012; invariantes 1, 4 e 7.

**Depende de:** S03-T06; interface de `GraphState` da trilha A; ADR-0012 (onda 0).

**Arquivos previstos:** criar `agents/normalizer.py`, `agents/prompts.py`, `config/prompts/normalizer.md`,
`domain/provenance.py`, `graph/nodes/normalizer.py`,
`src/fittrack/db/migrations/versions/_0006_raw_message_text_extract.py`,
`tests/unit/test_normalizer.py`, `tests/unit/test_provenance_alignment.py`,
`evals/golden/normalization.jsonl`; modificar `services/webhook.py`
(`SqlRawMessageStore.persist`).

**Migração (`_0006`, uma coluna, independente das outras da onda 0 — ver a nota de colisão de
numeração na T15).** `ALTER TABLE raw_message ADD COLUMN text_extract BYTEA;` — cifrada,
`NULL` quando a mensagem não carrega texto plano. Nenhuma coluna existente é, byte a byte,
`raw_fragment.text`: `payload` é o evento inteiro do canal serializado (ADR-0012, contexto #2).
`SqlRawMessageStore.persist` passa a cifrar `message.text` nesta coluna com o mesmo `column_aad` de
linha que já usa para `payload`, no mesmo `INSERT` — sem tocar `_envelope()` nem o formato do
Redis.

**Plano de implementação:** schemas Pydantic estritos para `TurnSegment` e `NormalizedTurn`; delimitar
fragmentos, transcrições, histórico e `pending_clarification` como dados não confiáveis — `clean_text`
**continua não confiável** e é redelimitado a jusante; validar que `source_fragments` aponta só para
índices existentes e que não há anáfora resolvida sem âncora; carregar `normalizer.md` no boot —
texto instrucional não fica em Python; golden set para fragmentação, STT, anáfora, interrupt pendente
e injection.

**`domain/provenance.py` nasce aqui, não na T09** (movido pela ADR-0012 após a revisão do PR): define
`SourceSpan` e duas funções determinísticas, `align_segment_spans` e `resolve_spans` — ver a ADR para
a assinatura completa. O motivo de nascer na T08: é o nó do normalizador, em
`graph/nodes/normalizer.py`, quem tem acesso simultâneo ao `raw_fragments` bruto do `GraphState`
(nunca exposto ao prompt) e ao `NormalizedTurn` que o LLM acabou de devolver. Logo depois da
validação Pydantic da saída do `NORMALIZER` — e antes de o nó devolver o estado — o nó chama
`align_segment_spans(segment, raw_fragments)` para cada `TurnSegment` e grava o resultado em
`segment.source_spans`, escolhendo `text_extract` ou `transcript` conforme `raw_fragment.was_audio`. É
código determinístico (`difflib.SequenceMatcher`, sem chamada de LLM): o passo que faz o
`extraction_agent` (T09) e o `guardrail_agent` (T23) poderem citar span sem nunca ter visto o texto
bruto — eles citam **índice de segmento**, este nó já resolveu o span de verdade.

**Contrato que a trilha A precisa:** `answers_clarification: bool` no `NormalizedTurn` — é o que
decide `Command(resume=...)` vs. invocação normal na T05.

**Primeiro teste que deve falhar.** `test_single_clean_fragment_is_lossless` com
`assert result.clean_text == "Supino 80 kg x 8"` e `assert result.segments[0].source_fragments == [0]`.

**Critérios de aceite:** rejeita saída fora dos `Literal`s e `source_fragments` inexistentes; teste de
injection confirma que instrução em fragmento não altera schema, papel, rota nem contexto; o prompt
existe, não é vazio, e não há instrução equivalente inline no módulo; `align_segment_spans` prova
`0 <= start < end <= len(texto-fonte)` em todo caso, inclusive quando a similaridade cai abaixo do
limiar e o resultado é o fragmento inteiro; um segmento gerado a partir de um fragmento
`was_audio=True` resolve contra `transcript`, nunca contra `payload` (o envelope inteiro).

**Tamanho:** M.

#### S03-T09 — Extraction schemas and RPE mapping

**Objetivo.** Turno normalizado → `ExtractionResult` validado, sem inventar campos. Single-shot, papel
`EXTRACTOR`. Representa `3x10` como `repeat=3`; a **persistência** expande em três linhas.

**Spec.** §9.1, §9.5, §9.6, §9.10, §22.3; AD-07, AD-35, AD-42; ADR-0012 e ADR-0018;
invariantes 1, 4, 6 e 7.

**Depende de:** S03-T06, S03-T08, ADR-0012 e ADR-0018 (onda 0).

**Arquivos previstos:** criar `agents/extraction.py`, `domain/rpe.py`, `domain/units.py`,
`config/prompts/extraction.md`, `tests/unit/test_extraction.py`, `tests/unit/test_rpe.py`,
`tests/unit/test_units.py`, `evals/golden/extraction.jsonl`. **Usa** (não recria)
`domain/provenance.py` — nasce na T08, ver ADR-0012.

**Plano de implementação:** schemas da §9.5 com `extra="forbid"`, limites de RPE, `repeat >= 1` e
`confidence` em 0..1. A saída crua do LLM não carrega `source_text`, `source_spans` nem valores já
convertidos: o extrator nunca viu o texto bruto (só o `NormalizedTurn`, §9.3) e um offset de
caractere que ele "adivinha" contra um literal que não leu não é verificável (ADR-0012). Cada
`ExtractedSet` carrega, em vez disso, `source_segments: list[int]` não vazia — índices em
`NormalizedTurn.segments`, o único texto que o prompt de fato recebeu por inteiro. Cargas, distâncias
e métricas que exigem normalização usam `QuantityLiteral(value: Decimal, unit:
Literal["kg", "lb", "m", "km", "cm", "h", "s", "min",
"scale_0_10"], unit_origin: Literal["explicit", "defaulted"])` — `s`/`min` cobrem `duration_s`,
`hold_s` e `rest_s` (ADR-0018): `"correu 40 minutos"` nunca chega como `duration_s=2400` calculado
pelo modelo. A validação pós-LLM confere que cada
índice de `source_segments` existe e então chama `domain/provenance.resolve_spans(...)` — os
`SourceSpan` já computados pela T08 para aqueles segmentos — para materializar `source_spans`
canônicos, ordenados e sem sobreposição; só então a proveniência imutável da ADR-0012 é passada
adiante para a T12. `source_text` não é prova de uma série: para um único span resolvido pode
receber aquele recorte literal; para múltiplos spans fica `NULL`, nunca uma concatenação de
`clean_text` ou a escolha enganosa de um fragmento.

`domain/units.py` é a única fronteira que converte `QuantityLiteral` em DTO de persistência
(`load_kg`, `distance_m`, `duration_s`/`hold_s`/`rest_s` ou métrica): usa `Decimal("0.45359237")`
para lb → kg, `Decimal("1000")` para km → m, `Decimal("60")` para min → s, e
`quantize(Decimal("0.01"), ROUND_HALF_UP)` **uma vez**, no limite de armazenamento — para os campos
`INTEGER` de tempo, `to_integral_value(ROUND_HALF_UP)` no lugar de `quantize` (ADR-0018). O prompt só
classifica e devolve o número literal mais a unidade; ele não calcula, não arredonda e não emite
`load_kg` para entrada em lb nem `duration_s` para entrada em minutos. `domain/rpe.py`, **e só ele** (ADR-0018), também aplica
`RIR ≈ 10 − RPE` apenas depois da resposta, para inferência derivada, preservando o RIR explícito.
Peso corporal, notação de séries e o mapa da §9.6 continuam no prompt; campo não dito vira
`missing_fields`, não chute (invariantes 1 e 6).

**Primeiros testes que devem falhar.** `test_extracts_repeat_and_rir_from_natural_language` com
`assert result.sets[0].repeat == 3`, `reps == 10`, `rir == 2`, `rpe == 8`; e
`test_set_assembled_from_three_fragments_resolves_ordered_validated_spans`, para `"supino"`, `"80 kg"`,
`"8 reps"`, onde o fake do LLM devolve só `source_segments` e o teste prova que `resolve_spans`
produz os três `SourceSpan` esperados, rejeitando `source_text` sintético.
`test_normalize_100_lb_with_decimal_half_up` afirma `Decimal("45.36")` no DTO de persistência e que o
fake do LLM devolveu somente `100` + `"lb"`.

**Critérios de aceite:** "muito fácil" → RPE 3; "falhei" → 10; RPE/RIR numérico explícito prevalece
sobre adjetivo; `3x10` continua uma unidade de extração (as três linhas físicas são provadas na T12);
`source_segments` inexistente, spans resolvidos fora do fragmento, sobrepostos ou fora de ordem,
campo inventado e valor inválido são
recusados **antes de tocar o banco**; testes de unidades cobrem kg/lb/km e min/s, constante e
arredondamento fixos (`quantize` para carga/distância, `to_integral_value` para os `INTEGER` de
tempo), e provam que nenhuma saída do LLM em lb ou em minutos chega a `load_kg`/`duration_s` sem
passar por `domain/units.py`.

**Tamanho:** M. Ver [ambiguidade #2](#2-rpe-e-rir-explícitos-e-contraditórios).

#### S03-T10 — Exercise catalog and vector index

**Objetivo.** O catálogo global curado e a coleção Qdrant mínima para a camada vetorial do resolver.
**Não** implementa RAG de conversas ou literatura.

**Spec.** §5.2, §10, §19.1; AD-08, AD-21; ADR-0003; invariantes 3 e 7.

**Depende de:** Sprint 01. Paraleliza com T06–T09.

**Arquivos previstos:** criar `data/exercises/global_catalog.json`, `scripts/seed_catalog.py`,
`repositories/exercises.py`, `rag/{embeddings,exercise_catalog}.py`,
`tests/unit/test_exercise_catalog.py`, `tests/integration/test_exercise_catalog.py`.

**Plano de implementação:** versionar ~300 exercícios globais com aliases normalizados, equipamento,
músculos e tipo de série; **executar seed com principal de migração/admin** — a RLS impede
corretamente que `fittrack_runtime` crie catálogo global; criar `exercise_catalog` com os 1.024
vetores de `config/rag.yaml`; seed idempotente por slug/alias; **o `bootstrap.py` não roda seed
implicitamente** — permanece explícito.

**Primeiro teste que deve falhar.** `test_seed_catalog_creates_global_aliases_and_qdrant_points` com
`assert global_aliases > 0` e `assert point_count == exercise_count`.

**Critérios de aceite:** seed repetido não duplica exercícios, aliases ou pontos; um tenant lê catálogo
global mas **não consegue criar ou alterar linha global**.

**Tamanho:** M. Ver [ambiguidade #15](#15-fonte-canônica-e-formato-do-catálogo-global).

#### S03-T11 — Safe exercise resolver

**Objetivo.** As quatro decisões online do §10: match exato, trigram, vetor e desempate LLM; depois
clarificação ou exercício privado pendente. **Determinístico**, com uma única chamada single-shot
`RESOLVER` no desempate — não é ReAct.

**Spec.** §9.2, §10, §19.1, §22.3; AD-08, AD-42; ADR-0003; invariantes 3 e 7.

**Depende de:** S03-T06, S03-T10.

**Arquivos previstos:** criar `agents/resolver.py`, `services/exercise_resolver.py`,
`config/prompts/resolver.md`, `tests/unit/test_exercise_resolver.py`,
`tests/integration/test_exercise_resolver_store.py`.

**Plano de implementação:** normalização lexical e camadas 1–3 com os limiares do §10, **sempre**
filtrando global ou o `tenant_id` já injetado; **só enviar ao LLM os cinco candidatos já autorizados
pela busca, e validar o `exercise_id` devolvido contra essa lista após o Pydantic** — um ID vindo do
modelo nunca vira query livre; delimitar nomes privados e contexto de sessão como não confiáveis,
sanitizando e truncando antes de criar exercício privado; gravar/incrementar alias `learned` privado
nas camadas 2/3/LLM, e no fallback criar exercício com `status='pending_review'` e alias
`source='user'`; **nunca copiar `exercise_tenant_id`, `is_bodyweight` ou `equipment` da saída do LLM**
— vêm da linha resolvida.

**Primeiro teste que deve falhar.**
`test_private_exercise_from_another_tenant_never_reaches_llm_candidates` com
`assert other_tenant_exercise_id not in fake_gateway.candidate_ids`.

**Critérios de aceite:** exato vence; trigram aceita `>=0.85`; vetor exige `>=0.88` e gap `>=0.06`;
LLM exige confiança `>=0.75`; exercício privado de outro tenant não pode ser lido, sugerido,
referenciado ou persistido; nome com injection permanece dado delimitado.

**Tamanho:** M. Ver ambiguidades [#9](#9-o-delta-de-empate-próximo-da-camada-trigram) e
[#10](#10-configpromptsresolvermd-não-está-na-23).

#### S03-T12 — Session manager and set persistence

**Objetivo.** Abrir/reabrir/reutilizar sessão e persistir séries resolvidas em transação
tenant-scoped. `repeat=3` gera **três** `exercise_set` com índices distintos (AD-07), não uma linha
agregada.

**Spec.** §5.2, §6.1, §6.2, §8.5, §9.2, §9.5, §9.10; AD-06, AD-07; ADR-0003; ADR-0012 (onda 0);
invariantes 3, 5 e 6.

**Depende de:** S03-T09, S03-T11, ADR-0012 e o contrato do subgrafo `ingestion` (T03).

**Arquivos previstos:** criar `domain/session.py`, `repositories/workouts.py`, `repositories/set_source.py`,
`services/ingestion.py`, `src/fittrack/db/migrations/versions/_0006_exercise_set_source.py`,
`tests/unit/test_session_manager.py`, `tests/integration/test_workout_persistence.py`,
`tests/integration/test_provenance_retention.py`.

**Migração (`_0006`, exigida pela ADR-0012 — esta é a peça que a versão anterior deste plano
omitia).** Cria `exercise_set_source` com as duas FKs tenant-qualificadas (`exercise_set_id` e
`raw_message_id`, a segunda com `ON DELETE SET NULL (raw_message_id)`), as chaves candidatas
`uq_exercise_set_id_tenant` em `exercise_set` e `uq_raw_message_id_tenant` em `raw_message`, e as
colunas novas em `exercise_set`: `provenance_hash BYTEA NOT NULL` e
`expansion_ordinal SMALLINT NOT NULL DEFAULT 0`. Substitui `ux_set_idempotency` — ver SQL completo na
ADR-0012.

**Plano de implementação:** criar/reabrir sessão conforme §6 — **somente `closed_auto` reabre até
15 min; `closed_explicit` não reabre**; derivar `is_bodyweight`, escopo e equipamento do registro no
banco; expandir `repeat` em `expansion_ordinal = 0..repeat-1`, marcar `inferred=True` nas repetições
implícitas. **A chave de idempotência não é a antiga.** `set_index` (exibição/ordenação) e
`provenance_hash`+`expansion_ordinal` (idempotência) são calculados separadamente e inseridos no
mesmo `INSERT`, mas só os dois últimos entram no alvo do `ON CONFLICT (session_id, exercise_id,
provenance_hash, expansion_ordinal) DO NOTHING` — **nunca** `ON CONFLICT` sobre `set_index`, porque
`set_index` é derivado da contagem atual de linhas da sessão, que o próprio INSERT anterior já
alterou: se o commit da tentativa 1 for bem-sucedido mas o checkpoint do grafo falhar antes de
persistir, a tentativa 2 (retry do mesmo batch) recalcula um `set_index` **diferente** — mas
`provenance_hash` e `expansion_ordinal` são funções só do conteúdo já validado da extração, então
continuam iguais, o `ON CONFLICT` colide e a tentativa 2 não insere nada (ver ADR-0012, seção
"Idempotência"). O `INSERT` de `exercise_set` usa `RETURNING id`; quando o conflito zera o
`RETURNING` (linha já existe de uma tentativa anterior), `repositories/workouts.py` faz um `SELECT`
pela mesma chave `(session_id, exercise_id, provenance_hash, expansion_ordinal)` para obter o `id`
existente — nunca insere `exercise_set_source` para um `exercise_set_id` inventado. Depois de cada
série resolvida (nova ou reencontrada), com `status='complete'` só quando o `CHECK ck_set_payload`
puder ser satisfeito, `repositories/set_source.py` insere uma linha em `exercise_set_source` por span
resolvido — mesma transação, mesmo `INSERT ... ON CONFLICT (exercise_set_id, position) DO NOTHING`,
para que o mesmo retry que reencontrou o `exercise_set` também não duplique a proveniência.

**Primeiro teste que deve falhar.** `test_persistence_expands_3x10_to_three_complete_rows` com
`assert rows == [(1, 10), (2, 10), (3, 10)]`.

**Critérios de aceite:** reprocessar a mesma entrada **não aumenta volume**; exercício global grava
escopo global e privado grava o do próprio tenant, com tentativa cruzada falhando; `v_set_volume`
contém as três séries completas e nunca uma incompleta; um teste de integração commita o `INSERT` de
`exercise_set` e simula falha de checkpoint **antes** de inserir `exercise_set_source` — o retry do
batch não duplica a série (prova direta do bug do `set_index` corrigido); FK cruzada entre tenants
para `exercise_set_id` e para `raw_message_id` reprovam nos dois sentidos; um teste de integração
apaga o `raw_message` referenciado (simulando a purga de 90 dias) e prova que `exercise_set_source`
sobrevive com `raw_message_id IS NULL`, `tenant_id` intacto e o snapshot (`channel`, `occurred_at`)
preservado.

**Tamanho:** M/G — cresceu com a migração da ADR-0012. **Bloqueado pela
[ambiguidade crítica #7](#7-crítico-uma-série-pode-vir-de-vários-fragmentos-mas-source_message_id-é-escalar).**

#### S03-T13 — Clarification and incomplete recording · **adiada para a Sprint 04**

**Objetivo.** A política de **uma pergunta agregada, uma tentativa, timeout de 20 min**. Single-shot
`ROUTER`; produz conteúdo estruturado, e a trilha A chama `interrupt()`.

**Spec.** §8.5, §8.7, §9.2, §9.10, §13.2; AD-35, AD-42; invariantes 2, 6, 7 e 11.

**Depende de:** S03-T08, S03-T09, S03-T11, S03-T12; checkpointer/interrupt (T04/T05); `voice`/`deliver`
(T19/T23).

**Arquivos previstos:** criar `agents/clarification.py`, `config/prompts/clarification.md`,
`tests/unit/test_clarification.py`, `tests/integration/test_incomplete_sets.py`.

**Plano de implementação:** resolver exercício **antes** de decidir campos obrigatórios — carga é
obrigatória só para força externa; retornar uma única `ClarificationRequest` agregada, com opções
fechadas quando houver até três candidatos plausíveis; após resposta ainda insuficiente ou timeout,
persistir o melhor palpite com `status='incomplete'` (invariante 6: falhar registrando);
**nunca enfileirar saída diretamente** — o resultado vai ao estado interno, `voice` decide e `deliver`
enfileira.

**Primeiro teste que deve falhar.**
`test_clarification_timeout_records_incomplete_set_without_discarding_raw_message` com
`assert stored.status == "incomplete"` e `assert await raw_message_store.exists(raw_id)`.

**Critérios de aceite:** rajada com quatro lacunas gera **uma** pergunta, não quatro; séries completas
da mesma rajada são persistidas **antes** do interrupt; timeout não apaga `raw_message` e a série fica
fora de `v_set_volume`; nenhum módulo em `agents/` ou subgrafo importa `channels` nem lê `channel_caps`.

**Tamanho:** M.

#### S03-T14 — Session lifecycle maintenance · **adiada para a Sprint 04**

**Objetivo.** Fechamento por inatividade, duração máxima, virada local do dia e descarte de sessão
vazia. Determinístico — não é agente.

**Spec.** §3.1, §6.1–6.4, §19.1; AD-06; invariantes 3 e 5.

**Depende de:** S03-T12.

**Arquivos previstos:** criar `services/session_maintenance.py`, testes unit/integration; modificar
`scheduler.py`, `settings.py`, `.env.example`, `docker-compose.yml`.

**Plano de implementação:** acrescentar as configurações ausentes (empty timeout 30 min, reabertura
15 min, boundary local 04:00); executar a cada minuto com **relógio injetável**; fechar `closed_auto`
por 90 min / 4h / boundary; descartar vazia após 30 min; publicar apenas IDs/eventos internos para o
resumo; **preservar RLS** — a varredura global não pode usar repository sem tenant.

**Primeiro teste que deve falhar.**
`test_idle_open_session_closes_without_exposing_other_tenant_rows` com
`assert session.status == "closed_auto"` e `assert foreign_session.status == "open"`.

**Critérios de aceite:** cada guarda da §6.2 tem teste com tempo controlado; `closed_auto` reabre só
dentro de 15 min e explícita não; **o scheduler não recebe poder de leitura arbitrária sobre dados de
treino**.

**Tamanho:** M. **Bloqueado pela [ambiguidade crítica #8](#8-crítico-o-scheduler-precisa-varrer-todos-os-tenants-e-a-rls-corretamente-o-impede).**

### Trilha C — Saída, entrega e as issues

#### S03-T15 — Outbound claim function (SECURITY DEFINER) · issue #29 (item 1)

**Objetivo.** A única porta pela qual um worker reivindica linhas devidas de `outbound_queue` —
função SQL `SECURITY DEFINER` com `FOR UPDATE SKIP LOCKED`, lease e ordenação de grupo.

**Por que função e não query.** Com RLS ativo, uma sessão sem `app.tenant_id` enxerga **zero linhas**
de `outbound_queue` — a policy da `_0002` compara `tenant_id = NULLIF(current_setting(...))`, e `NULL`
nunca é verdadeiro. **A query global de elegibilidade da §18.4 não pode ser emitida pela aplicação.**
Precisa da fronteira pré-tenant da §19.1. Este é o achado que une esta tarefa à T18 e às
[ambiguidades #5 e #8](#a-fronteira-de-manutenção-cross-tenant). E trocar o papel efetivo não basta:
`FORCE ROW LEVEL SECURITY` vale também para o dono da tabela, e uma função `SECURITY DEFINER` de dono
`NOBYPASSRLS` continuaria vendo zero linhas — o mecanismo efetivo da fronteira é o `BYPASSRLS` do dono,
num papel `NOLOGIN` de raio limitado por grants, exatamente como a `fittrack_identity` da `_0002`
(o comentário da migração é explícito: `BYPASSRLS` existe "so the two functions can see
`channel_identity` before there is an `app.tenant_id` to see it with").

**Spec.** §18.4, §17.4, §19.1, §5.2. Issue #29.

**Depende de:** nada fora da trilha.

**Arquivos previstos:** criar `db/migrations/versions/_0006_outbound_claim.py` (role
`fittrack_outbound` `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS` — espelho da
`fittrack_identity` da `_0002:129`, porque sem o `BYPASSRLS` efetivo a função vê zero linhas; função
`claim_due_outbound(p_now, p_lease, p_limit)`, grants coluna-a-coluna no estilo `_0004`,
`REVOKE ... FROM PUBLIC` + `GRANT EXECUTE TO fittrack_app` + `ALTER ... OWNER` + a guarda da `_0002`
que revoga qualquer membro do papel, para o `BYPASSRLS` não ficar alcançável por `SET ROLE`) e
`tests/integration/test_outbound_claim.py`; modificar `services/outbound.py` (`claim_due` no store e
no protocolo).

> **Colisão de numeração de migração:** quatro tarefas de trilhas paralelas propõem migração sem
> coordenar número entre si — T15 (`_0006_outbound_claim`), T08
> (`_0006_raw_message_text_extract`, ADR-0012) e T12 (`_0006_exercise_set_source`, ADR-0012), além
> de T23 (`_0007_health_report_idempotency`) e T18 (`_0007_membership_identity_resolution`). Nenhuma
> delas depende do conteúdo de outra, então não há ordem "correta" — só ordem de merge. **Resolver
> no merge, não na branch:** a primeira PR a mergear fica com o número que já tem; cada PR seguinte
> cujo número colide renumeia o próprio arquivo (nome e `down_revision`) para o próximo livre antes
> de abrir o merge. É renumeração mecânica, nunca reordenação de conteúdo.

**Desenho.** A função executa a query da §18.4 (elegível = `sent_at IS NULL AND dead_at IS NULL AND
scheduled_at <= now AND next_retry_at <= now`, mais o `NOT EXISTS` que só libera `seq n+1` quando
`seq n` do grupo tem `sent_at`), com `ORDER BY group_id, seq LIMIT :limit FOR UPDATE SKIP LOCKED`, e
**usa o próprio `next_retry_at` como lease**: um `UPDATE ... RETURNING` no mesmo statement promove
`next_retry_at = p_now + p_lease`. Worker morto se cura sozinho quando o lease expira. **Nenhuma
coluna nova.** Ver [ambiguidades #11 e #12](#11-lease-via-next_retry_at-vs-coluna-claimed_at).

**Primeiro teste que deve falhar.** `test_concurrent_claim_sessions_never_return_the_same_row` — duas
sessões reais chamam a função sobre as mesmas linhas elegíveis; conjuntos de `id` disjuntos e cada
linha com lease promovido.

**Critérios de aceite:** linha com `scheduled_at` futuro não é reivindicada; `sent_at`/`dead_at`
preenchidos nunca; `seq 1` não sai enquanto `seq 0` do grupo não tem `sent_at`; **teste provando que
`fittrack_runtime` não lê `outbound_queue` sem `app.tenant_id` e só alcança linhas via a função**;
atributos da role e ausência de membros provados por teste, no padrão da guarda da `_0002`;
payload e `external_id` voltam cifrados; `alembic upgrade`/`downgrade` idempotentes.

**Tamanho:** M.

#### S03-T16 — Drain job: nothing calls `OutboundService.deliver()` anymore · issue #29 (item 2)

**Objetivo.** Um job ARQ `drain_outbound` que reivindica lotes via `claim_due`, resolve
`ChannelIdentity`, escolhe o adaptador no `ChannelRegistry` e chama `OutboundService.deliver()` em
laço — mais o cron que o mantém rodando e o gatilho imediato para o caso interativo.

**Estado que isto corrige.** Hoje **nenhum job chama `OutboundService.deliver()`**. Tudo que
`VoiceIngestion._refuse` enfileira fica persistido e nunca é enviado, e `enqueue_unsupported_media`
não tem chamador. É o coração da issue #29.

**Spec.** §18.4, §17.2, §17.4, §18.2. Issue #29.

**Depende de:** S03-T15.

**Arquivos previstos:** criar `services/drain.py`, `tests/unit/test_outbound_drain.py`,
`tests/integration/test_outbound_drain.py`; modificar `worker.py`.

**Plano de implementação.** `cron(drain_outbound, minute=set(range(60)), second={0,15,30,45},
run_at_startup=True)` — o arq 0.26.3 suporta `second=`, então drenagem sub-minuto é possível. **Mais
um gatilho `drain:kick`**: a §4 mostra o POST ao Telegram ~1–2s depois do `deliver`, e só o cron de
15s não entrega isso — o nó `deliver` (T19) enfileira o kick, e o cron é a rede de recuperação
(retry, lease expirado, kick perdido). Falha de uma linha nunca derruba o job: `deliver()` já
classifica e persiste o desfecho; só erro de infra propaga.

**Primeiro teste que deve falhar.** `test_drain_delivers_a_due_row_and_marks_it_sent` — canal falso
conta `send`; uma linha elegível é entregue e `mark_sent` gravado com `attempts=1`.

**Critérios de aceite:** enqueue → drain → canal chamado → `sent_at` persistido; `RETRY_BACKOFF` grava
a escada 2s/8s/32s/2min/8min com jitter ±25%; `RETRY_AFTER` usa o valor literal; classe não-repetível
grava `dead_at` + cascade do grupo; `UNDELIVERABLE` revoga a identidade; `proactive=True` nunca
repete; duas execuções concorrentes não enviam a mesma bolha; worker morto durante o envio tem a
linha reentregue **uma vez** após o lease; nenhum `external_id`/`file_path`/texto de resposta em log
ou span.

**Tamanho:** M.

#### S03-T17 — Unsupported-media reply wired at the ingress · **cortada — absorvida pela T28**

**Objetivo.** `image`/`document` deixam de ser descartados em silêncio. Hoje `_is_processable`
devolve `False` para os dois: são persistidos em `raw_message` e sumem — e
`test_non_processable_updates_are_persisted_but_not_buffered` **fixa esse comportamento**.

**Spec.** §18.2, §18.4, §17.4; ADR-0009. Issue #29.

**Depende de:** nenhuma obrigatória (o enqueue já existe); aceite fim-a-fim depende de T16.

**Arquivos previstos:** modificar `services/webhook.py` (colaborador opcional
`UnsupportedMediaReplier`, no mesmo padrão do `revoker`), `ingress_wiring.py`,
`tests/integration/test_webhook_ingress.py`; criar `tests/unit/test_ingress_wiring.py`.

**Plano de implementação.** **`group_id` derivado** — `uuid5(namespace, f"{raw_message_id}:unsupported_media")`,
espelhando o `_reply_group` de `stt.py` — para que a reentrega que refaz o insert de `raw_message`
(mesmo id, `ON CONFLICT DO NOTHING`) não duplique a resposta. `UNIQUE (group_id, seq)` vira a barreira.

**Primeiro teste que deve falhar.** `test_an_image_is_persisted_and_answered_with_the_fixed_degradation`.
Falha hoje porque o teste existente prova o silêncio.

**Critérios de aceite:** foto e documento geram uma resposta enfileirada cada, com o texto de
`config/prompts/unsupported_media.md`; reentrega do mesmo `update_id` **não** duplica;
`text`/`voice`/`button_reply` seguem intocados; `message_reaction` e `my_chat_member` não geram
resposta. Ver [ambiguidade #16](#16-answered_at-como-segundo-marcador-da-resposta-de-mídia).

> **CORTADA pela [decisão #26](#26-crítico-de-escopo--t17-e-t28-escrevem-e-apagam-o-mesmo-código).**
> Esta tarefa enfileirava **fora do grafo**, sob a autorização temporária do ADR-0009; a T28 desliga
> exatamente esse caminho e manda imagem e documento pelo evento de controle `unsupported_media` até o
> `voice`. Fazer as duas seria escrever e apagar o mesmo código. **O texto abaixo fica como registro** —
> em especial o `group_id` derivado, que a T28 reaproveita.

**Tamanho:** P.

#### S03-T18 — Membership-revocation identity resolution · issue #30

**Objetivo.** Um caminho de resolução para eventos de revogação que encontra a identidade existente
**independentemente de `revoked_at`** — a reentrega de um `my_chat_member` kicked/left nunca mais
cunha tenant órfão.

**O bug, confirmado no código.** A sequência do `accept()` é `resolve_or_create` → persiste raw →
`revoke_identity` (commit próprio) → invalidação de cache → `deduplicator.complete`. Qualquer exceção
depois do commit da revogação libera a reserva, e a reentrega roda `resolve_or_create` contra a única
função existente — `resolve_tenant_for_identity`, que filtra `revoked_at IS NULL` — e cunha tenant +
identidade novos. O comentário em `webhook.py` já registra isso como débito.

**Spec.** §18.2, §19.1, §5.2. Issue #30.

**Depende de:** nada. **Paraleliza com tudo** — pode subir primeiro se o time priorizar o bug latente.

**Arquivos previstos:** criar `db/migrations/versions/_0007_membership_identity_resolution.py`
(função `resolve_tenant_for_membership_event(p_channel, p_external_id_hash, p_channel_message_id)`,
**só `SELECT`** — e é esse "nada a criar" que mata o órfão —, resolvendo em dois degraus: primeiro
pela **cópia persistida do evento**, `SELECT rm.tenant_id, rm.identity_id FROM raw_message AS rm JOIN
channel_identity AS ci ON (ci.id, ci.tenant_id, ci.channel) = (rm.identity_id, rm.tenant_id,
rm.channel) WHERE rm.channel = p_channel AND rm.channel_message_id = p_channel_message_id AND
ci.external_id_hash = p_external_id_hash ORDER BY rm.id LIMIT 1`. O join e o predicado do hash são
obrigatórios: `channel_message_id` identifica o update dentro de uma conta de bot, e pode repetir
depois de rotação do token; uma cópia só amarra a reentrega se também pertencer à identidade externa
recebida. A função pertence à fronteira `fittrack_identity` da `_0002`; a migração concede a ela
somente `SELECT (id, tenant_id, identity_id, channel, channel_message_id)` em `raw_message`. Sem cópia
da **mesma identidade externa**, o segundo degrau escolhe a linha mais recente de `channel_identity`
para `(channel, external_id_hash)`, revogada ou não) e
`tests/integration/test_membership_revocation.py`; modificar `services/identity.py`,
`services/webhook.py`, testes de ingress e de cache.

**Plano de implementação.** Em `accept()`, eventos de revogação usam o resolvedor novo **em vez de**
`resolve_or_create`, com o `channel_message_id` do evento (`telegram-update:<id>`): sem linha → evento
sem efeito (nada a revogar; log + métrica); com linha → persiste raw sob aquele tenant — a cópia antiga
é devolvida pelo `ON CONFLICT (identity_id, channel_message_id)`, não duplicada —, revoga aquele
`identity_id` (idempotente via `coalesce`), invalida cache. O `CachedIdentityResolver` **não** preenche
o cache ativo com identidade revogada. **Por que a cópia persistida decide, e não "a mais recente":**
a tentativa original de um `kicked` sempre persiste o raw **antes** de revogar, então a cópia amarra o
evento à identidade de origem. Escolher cegamente a linha mais recente escolheria a identidade
**fresca e ativa** que uma mensagem posterior criou — o usuário desbloqueou, mandou mensagem, e a
reentrega do `kicked` vencido revogaria a identidade nova. O fallback "mais recente, revogada ou não"
só roda quando não há cópia nenhuma — evento nunca persistido, logo sem revogação anterior a respeitar.

**Primeiro teste que deve falhar — e deve falhar pelo bug real.**
`test_a_redelivered_block_event_does_not_mint_a_second_tenant` — entrega um `my_chat_member kicked`,
força falha no `deduplicator.complete`, reentrega o mesmo evento; `count(*) FROM tenant` não cresce e
a identidade continua exatamente uma. **Rodar contra o código atual prova o órfão.**
`test_a_kicked_event_retried_after_unblock_targets_the_original_identity` — processa o `kicked`,
desbloqueia, manda mensagem (identidade nova e ativa), reentrega o `kicked` antigo; a revogação recai
sobre a identidade **original** — decidida pela cópia em `raw_message`, não pela linha mais recente —,
a identidade nova permanece ativa e não ganha cópia do evento.
`test_a_reused_update_id_after_bot_rotation_stays_scoped_to_the_external_identity` — deixa uma cópia
`telegram-update:N` para a identidade A, recebe do bot novo um evento com o mesmo `update_id` para a
identidade B e prova que o resolvedor não devolve a linha de A; o fallback resolve B e só B é revogada.

**Critérios de aceite:** reentrega persistente não cria segundo tenant nem segunda identidade; a
função devolve a cópia original do evento quando ela existe **para o mesmo `(channel,
external_id_hash)`** e, sem essa cópia, a linha mais recente mesmo revogada — vazio para `(channel,
hash)` sem linha alguma; colisão de `channel_message_id` entre hashes externos nunca atravessa
identidades; grants exatos provados por teste; a tabela de "identidade revogada" em
`test_identity_bootstrap.py` continua
verde; **unblock + nova mensagem continua criando identidade nova** (§18.5 preservada) — só o *evento
de revogação* usa a função nova, e a reentrega de um `kicked` após o desbloqueio **não** revoga a
identidade nova.

**Tamanho:** M.

#### S03-T19 — The `deliver` graph node: the single enqueuer

**Objetivo.** O nó `deliver`: converte a decisão do `voice_agent` em `OutboundBlock`s, atribui
`group_id`/`seq`/`scheduled_at` (escadinha do split, §13.6), enfileira via `OutboundService` e dispara
o drain. É o **único** enfileirador; o drain (T16) é o único que fala com a API — as duas metades da
invariante 2 fechadas em código.

**Spec.** §8.3, §8.4, §13.6, §4.2, §18.1/AD-39 — **o nó `deliver` não lê `channel_caps`**; quem lê é o
`voice_agent` e o adaptador de saída.

**Depende de:** S03-T16 (drain), trilha A (state/topologia), **S03-T21** (contrato `VoiceOutput`) e
S03-T26 (o nó que o produz). O teste unitário do nó não depende delas; a integração no grafo sim.

> A trilha C escreveu "S03-T23" aqui ao chutar a numeração da trilha D antes de ela existir. O
> contrato `VoiceOutput` é a **T21**; a T23 é o `guardrail`. Corrigido na consolidação.

**Arquivos previstos:** criar `graph/nodes/deliver.py` (o caminho **já está reservado** como exceção
em `tests/test_channel_isolation.py`) e `tests/unit/test_deliver_node.py`; modificar a topologia raiz
junto com a trilha A, `channels/telegram/adapter.py` e `tests/unit/test_telegram_adapter.py`.

**Plano de implementação.** `scheduled_at = now + Σ delays` implementa o espaçamento do §13.6 (o rate
limiter por chat é o piso de 1s em runtime); reação de ack vira `OutboundBlock(kind="reaction",
emoji=…, text=fallback_text, reply_to=…)`; toda chamada a `enqueue_response()` recebe
`identity_id=state["destination_identity_id"]`, carregado do último `raw_message_id` pela T05 — nunca
faz lookup por `(channel, reply_to)`. Ao fim, enfileira `drain:kick` por porta injetada — **best-effort**:
kick que falha loga e não derruba o nó, porque o cron do drain (T16) é a rede de recuperação e um kick
re-disparado pelo retry é inócuo.

**A degradação de reação fecha no adaptador.** `deliver` copia `VoiceOutput.fallback_text` para
`OutboundBlock.text` e persiste o campo cifrado com o restante do payload. Quando o Telegram rejeita
o emoji ou a mensagem-alvo já desapareceu, `TelegramAdapter._send_degraded()` envia `block.text`, não
o próprio emoji; o emoji só permanece como fallback de compatibilidade para uma linha antiga sem
`text`. O adaptador continua decidindo **quando** degradar, porque isso é protocolo; o `voice` continua
sendo o único autor de **o que dizer**, porque isso é conteúdo (invariantes 2 e 11). Os dois caminhos
de falha têm teste no adaptador. Um `VoiceOutput(mode="media")` que atravesse essa fronteira por bug
falha explicitamente antes do enqueue; não vira silêncio. A T26 garante que progresso com gráfico
chega aqui como texto nesta fase.

**O `group_id` é derivado, nunca sorteado.** O padrão de `enqueue_response()` gera um UUID fresco
(`services/outbound.py:299,332`), e o registro do nó dá `RetryPolicy(max_attempts=3)`: enqueue
commitado + kick ou write de checkpoint falho → o retry do nó mintaria um **segundo** grupo e as duas
cópias sairiam. Toda resposta do grafo chega com `group_id = uuid5(namespace fixo, f"{batch_id}")` —
o `batch_id` é entrada imutável do estado (§8.2), idêntico em toda repetição do nó e em todo
reprocesso do batch (§18.4), e a `UNIQUE (group_id, seq)` da §5.2 transforma a segunda tentativa em
no-op, não em bolha nova. Mesmo padrão do `_reply_group` do STT (`stt.py:1122`) e do derivado da T17.

**Primeiro teste que deve falhar.**
`test_deliver_enqueues_one_group_per_response_with_staggered_bubble_scheduling` — 3 bolhas → 1
`group_id`, `seq` 0..2, `scheduled_at` crescente com piso de 1s, `reply_to` e
`destination_identity_id` presentes, kick disparado.

**Critérios de aceite:** nenhum enfileiramento fora de `deliver` — `make test-architecture` verde com
o nó presente; e2e (com A+B+D): rajada → batch → grafo → `voice_agent` → `deliver` → drain → bolhas no
fake de canal, na ordem e espaçadas; o nó que falha **depois** do enqueue (kick ou checkpoint) e é
repetido não cria segundo grupo — a segunda execução cai no `ON CONFLICT (group_id, seq) DO NOTHING`;
`RetryPolicy(max_attempts=3)` no registro do nó; reação rejeitada pelo conjunto do Telegram e reação
cujo alvo desapareceu enviam o `fallback_text` redigido pelo `voice`; `mode="media"` inesperado falha
alto e não enfileira nem descarta silenciosamente; duas identidades com o mesmo
`channel_message_id` provam que o enqueue recebe exatamente a identidade da última mensagem da rajada.

**Tamanho:** M isolado; G contando a integração.

#### S03-T20 — PII redaction list, verified by test · **adiada para a Sprint 04**

**Objetivo.** A lista de redação da §20.2 como código + processador de spans aplicado antes de
qualquer export. `external_id` (o `chat.id`) e `telegram.file_path` (que carrega o token do bot na
própria URL) **nunca saem**.

**Spec.** §20.2, §20.6, §18.2; invariante 10.

**Depende de:** S03-T16, S03-T19 (nos pontos onde emite).

**Arquivos previstos:** criar `observability/redaction.py` e `tests/unit/test_pii_redaction.py`;
modificar `services/drain.py` e `graph/nodes/deliver.py`.

**Primeiro teste que deve falhar.** `test_external_id_file_path_and_payload_are_always_redacted`.

**Critérios de aceite:** teste parametrizado cobre **cada** item da lista da §20.2; span do drain
semanticamente vazio de conteúdo (só duração/status); atributo novo fora da allowlist reprova.

**Tamanho:** P.

### Trilha D — Agentes de decisão e saída

Esta trilha era o buraco da divisão original: `router` (§9.4), `guardrail` (§9.2), `voice_agent`
(§13), `correction` e `summary_agent` não pertenciam a nenhuma fatia — a trilha B os listou como
"fora desta slice", a A os deixou como *stubs*, e a C ficou dependendo de um contrato `VoiceOutput`
que ninguém tinha definido. Ela fecha esse buraco e entrega o único artefato que a trilha C pode
consumir: `state.voice_output` validado.

O `voice_agent` é o nó de maior consequência da sprint: é o único que **decide** o que o usuário vê,
e um dos dois únicos lugares autorizados a ler `channel_caps` (invariantes 2 e 11, AD-39). É também
a peça que permite resolver o ADR-0009.

**Duas restrições físicas, não estilísticas.** `tests/test_channel_isolation.py` já reserva
exatamente `graph/nodes/voice.py` e `graph/nodes/deliver.py` como as duas exceções. Logo
`agents/voice.py` pode conter schema e prompt, mas a leitura de `channel_caps` tem de ficar
fisicamente em `graph/nodes/voice.py` — qualquer módulo em `agents/` que leia essa chave quebra o
teste. E o schema canônico não importa `fittrack.channels`: a conversão de `ChannelCaps` em limites
de renderização acontece no nó, não no contrato.

#### O contrato `VoiceOutput` — a fronteira D → C

Chave nova no estado, **escritor único, sem reducer**:

```python
# src/fittrack/graph/state.py
class GraphState(TypedDict):
    destination_identity_id: int                    # entrada, identidade da última mensagem
    outbound: Annotated[list[dict], resettable_add] # entrada do voice, muitos escritores
    voice_output: dict[str, object] | None          # saída do voice, um escritor
```

`outbound` tem reducer porque muitos subgrafos escrevem nela em paralelo. Misturar nela a saída
final permitiria mais de um escritor e apagaria a fronteira entre **conteúdo semântico** e **formato
de canal**. Nem `deliver`, nem subgrafo, nem retry escreve `voice_output`; o nó `voice` é seu único
escritor, e `deliver` a revalida com `VoiceOutput.model_validate(...)` antes de enfileirar.

A chave guarda `model_dump(mode="json")`, não a instância Pydantic — é a convenção da própria §8.2
(`turn`, `analysis_result`, `recommendation` como `dict`) e evita depender de serialização de objeto
no checkpoint PostgreSQL. **Todo consumidor revalida; ninguém confia num `dict` vindo do
checkpoint.**

| `mode` | `deliver` enfileira |
| --- | --- |
| `reaction` | um `OutboundBlock(kind="reaction", emoji=…, text=fallback_text, reply_to=state["reply_to"])` — o `text` do bloco só é usado pelo adaptador após falha de protocolo (decisão #24, ADR-0015) |
| `text` | `split` se existir, senão `[text]`, em um grupo com `seq` crescente |
| `buttons` | um `OutboundBlock(kind="buttons", text=…, buttons=tuple(…), reply_to=…)` |
| `media` | **nenhum enqueue e erro explícito nesta fase** — `enqueue_response()` recusa `media_path` local (`services/outbound.py:330`, ADR-0005); pelo caminho durável o `voice` degrada o gráfico de progresso para texto — a leitura vai, o PNG espera blob storage durável (condição de revisão do ADR-0005) |
| `silent` | nenhum bloco, nenhuma chamada de enqueue |

O nó `voice` valida **antes** de escrever a chave que `len(buttons) <= caps.max_buttons`,
`len(split) <= caps.max_bubbles`, cada bolha cabe em `caps.text_limit` e a legenda em
`caps.caption_limit`. O adaptador continua responsável apenas por tradução de markup e falha de
protocolo — nunca por decidir conteúdo.

#### S03-T21 — Contratos de decisão, `VoiceOutput` e extensão do `GraphState`

**Objetivo.** Criar os schemas Pydantic que cruzam a fronteira dos agentes de decisão e declarar
`voice_output` como a saída final, serializável e de escritor único do nó `voice`.

**Spec:** §§8.2, 8.8, 9.2, 9.4, 13.1, 13.6, 18.1, 20.2.

**Depende de:** S03-T01 (`GraphState` e teste de reducers). **Bloqueia** T19 da trilha C, T22, T26 e
T28 — é o contrato que todo mundo espera.

**Arquivos previstos:** criar `agents/voice.py`, `agents/router.py`, `tests/unit/test_voice_contract.py`,
`tests/unit/test_router_contract.py`; modificar `graph/state.py`, `tests/test_graph_reducers.py`,
`agents/__init__.py`.

**Plano.** `Target`, intents fechados e `RouteStep`/`RoutingPlan` validados contra a tabela da §9.4,
com `extra="forbid"`. `VoiceOutput` com validação **por modo** (`reaction` exige emoji **e**
`fallback_text` — decisão #24/ADR-0015: quem redige o texto de degradação de uma reação recusada ou
sem mensagem-alvo é o `voice`; exigir que o adaptador invente palavra visível ao usuário viola a
invariante 2 — e proíbe split; `text` proíbe emoji/botões/mídia; `buttons` exige pergunta e opções;
`media` exige `media_path` e trata `text` como legenda opcional, mas **não é emitido nesta sprint**:
pelo caminho durável o enqueue recusa mídia local (ADR-0005), o progresso degrada para texto e
`deliver` rejeita explicitamente um `media` inesperado; `silent` não carrega nada). `voice_output`
declarado **sem** `Annotated`, com o escritor único documentado no próprio schema.

**Primeiro teste que deve falhar.** `test_reaction_shape_requires_an_emoji_and_fallback_text` — hoje
falha porque `VoiceOutput` não existe; depois vira `pytest.raises(ValueError)`, deixando o contrato
inválido explícito em vez de implícito.

**Critérios de aceite:** payload com campo extra, `reaction` sem `fallback_text` e toda combinação
inválida de modo reprovam;
`tests/test_graph_reducers.py` demonstra que só `outbound` usa reducer nesta fronteira;
`uv run mypy src/fittrack/agents src/fittrack/graph/state.py` passa.

**Tamanho:** M.

#### S03-T22 — `router_agent` e estagiamento determinístico

**Objetivo.** Produzir um `RoutingPlan` com o vocabulário fechado da §9.4 e transformar a proposta do
LLM em estágios seguros para `dispatch`/`Send`, **sempre** pondo escrita de ingestão antes de leitura.

**Spec:** §§8.2–8.4 (`Send`), 8.8, 9.1–9.4, 19.3, 20.3, 22.3.

**Depende de:** S03-T21, S03-T08 (normalizer), S03-T02 (root/`dispatch`), S03-T06 (gateway) e
S03-T07 (quota).

**Arquivos previstos:** criar `graph/staging.py`, `graph/nodes/router.py`, `config/prompts/router.md`,
`tests/unit/test_router.py`, `tests/unit/test_stage_quota.py`; modificar `agents/router.py`,
`graph/root.py`, `llm/roles.py`, golden set de roteamento.

**Plano.** Uma chamada ao gateway com `agent="router"`, `role=LLMRole.ROUTER`; nunca texto cru, nunca
`channel_caps`. **`stage_plan()` ignora qualquer ordenação sugerida pelo LLM:** se há `ingestion`, ela
é o estágio 1 sozinha e todo o resto forma o estágio 2 paralelo. `state["plan"]` só é escrito depois
de `stage_plan()`. Truncamento em quatro passos registra em `errors` e na métrica `agent_plan_steps`,
sem inventar destino alternativo. `pending_clarification` tem precedência: o worker retoma por
`Command(resume=…)` e o router se recusa a executar.

Antes do primeiro `Send` de **cada** estágio, `dispatch` chama
`QuotaService.reserve_stage(tenant_id, batch_id, stage_cursor, estimates)`. `estimates` enumera cada
passo e o máximo de invocações que seu contrato permite (incluindo as 3 voltas de `analysis` e
tentativas/fallback); usa teto de tokens/configuração e o provider mais caro elegível, nunca uma
média otimista. Cada item explicita custo `0` quando o papel é rápido; os itens `ANALYST`/`COACH`
entram na reserva de USD e `analysis_calls`.

**A reserva é por passo, nunca por estágio inteiro.** A versão anterior deste plano somava todos os
passos do estágio (`total_stage`) numa única checagem atômica e, sem saldo, suprimia o `Send` de
**todo** o estágio — inclusive os passos de custo `0`. Isso viola a própria regra da §19.3 que o
parágrafo cita ("não bloquear registro"): um estágio paralelo mistura livremente ramos caros
(`analysis`/`recommendation`) com ramos gratuitos (`admin/change_persona`, smalltalk), porque
`stage_plan()` os agrupa por *estágio de leitura*, não por custo. `reserve_stage` particiona
`estimates` em dois grupos antes de reservar: passos com custo `0` são incluídos no `Send` list
incondicionalmente, sem entrar no script atômico — não há o que reservar; passos `ANALYST`/`COACH`
passam pelo script Redis atômico sobre `quota:{tenant_id}:{yyyy-mm}`, que confere e reserva, numa
única operação, `used + reserved + total_priced` e `analysis_calls` **só da soma dos passos
cobrados**. Falta de saldo omite **apenas os passos cobrados** do `Send` list daquele estágio — os
zero-custo do mesmo estágio saem normalmente — e registra `QuotaExceeded` só para o que foi omitido.
A chave idempotente `(tenant_id, batch_id, stage_cursor)` retorna a mesma reserva (ou a mesma
omissão) em retomada de checkpoint, sem cobrar duas vezes nem reavaliar saldo que já mudou. O
`reservation_id` e a parcela do ramo seguem no payload privado do `Send` de cada passo cobrado; o
gateway consome e confere cada chamada contra essa parcela, lança a verificação individual como
defesa em profundidade e liquida custo real ou libera o saldo não usado quando o ramo termina.
Retry do mesmo batch reutiliza a reserva até o estágio fechar; falha terminal do ramo ou do batch libera
somente a parcela ainda reservada, para que uma queda de worker não transforme quota em crédito preso.

**A liberação acima depende de algo rodar até o fim — e um worker morto no meio (kill -9, OOM, última
tentativa do ARQ perdida) não roda nada.** Sem mais nada, a reserva fica presa no hash mensal
`quota:{tenant_id}:{yyyy-mm}` pelo resto do período, bloqueando o saldo pago do tenant sem ele ter
consumido nada. `reserve_stage` também grava o `reservation_id` num ZSET
`quota:reservations:{tenant_id}:{yyyy-mm}`, pontuado pelo deadline do lease (mesmo idioma do
`interrupts:pending` da T05 — ver [ambiguidade #6](#6-um-interrupt-expirado-no-redis-não-é-varrível)),
com um lease fixo (15 min — folgado acima do pior caso de duas tentativas + fallback + overhead de
avaliação de um estágio). `reserve_stage` começa **sempre** varrendo `ZRANGEBYSCORE 0 now` do próprio
ZSET do tenant que está chamando — sem varrer outros tenants, sem precisar de scheduler nem de
`SECURITY DEFINER`, porque cada tenant só varre a própria fila ao fazer sua própria próxima chamada —
e libera atomicamente, no mesmo script Lua, qualquer reserva vencida antes de conceder uma nova. Isso
autocura o caso comum: um tenant ativo que manda outra mensagem depois da falha já limpa o crédito
preso antes de gastar de novo. Um tenant que nunca mais manda mensagem naquele mês não perde nada além
do já perdido (o hash mensal expira sozinho na virada); o `ADR-0010`/scheduler da T14 (Sprint 04) pode
somar uma varredura periódica como reforço, mas o lease + autocura no próprio `reserve_stage` já fecha
o caso descrito pela revisão sem exigir a fronteira cross-tenant desta sprint.

**Primeiros testes que devem falhar.** `test_stage_plan_puts_ingestion_before_readers` — plano com
`analysis` antes de `ingestion` sai estagiado com ingestão sozinha no estágio 1; e
`test_dispatch_reserves_only_priced_steps_before_any_send`, com duas estimativas cobradas que,
juntas, excedem o saldo e `assert priced_sends == []`.

**Critérios de aceite:** os cinco alvos e todos os intents permitidos passam, intent incompatível
reprova; teste concorrente prova que duas tentativas de reservar o mesmo último saldo não passam
ambas, e teste de retomada prova que a reserva idempotente não duplica `reserved_usd`; `test_graph_topology`
prova `router → dispatch` e os cinco destinos; `test_channel_isolation` passa. T07 mantém testes do
gateway para chamada sem parcela ou acima dela: a reserva pré-fan-out complementa, não substitui, a
guarda por chamada; falha terminal libera saldo não consumido; `test_reservation_past_lease_is_reclaimed_on_next_call`
prova que uma reserva com deadline vencido é liberada atomicamente na chamada seguinte de
`reserve_stage` do mesmo tenant, antes de qualquer checagem de saldo nova;
`test_zero_cost_step_dispatches_when_priced_sibling_is_unaffordable` prova que um estágio com
`analysis` (sem saldo) e `admin/change_persona` (custo `0`) despacha o segundo mesmo quando o
primeiro é omitido por `QuotaExceeded`.

**Tamanho:** M.

#### S03-T23 — `guardrail_agent` entre normalização e roteamento

**Objetivo.** Triagem estruturada de saúde/segurança em toda rajada, retornando
`Command[Literal["router", "voice"]]` e **preservando o registro de treino** quando houver
`HEALTH_REPORT`.

**Spec:** §§8.3, 8.4 (`Command`), 9.2, 12.1–12.3, 13.1, 13.2, 19.5, 20.2, 22.3. Invariante 2.

**Depende de:** S03-T02, S03-T06, S03-T08, S03-T21, ADR-0012; T26 para a verbalização final.

**Arquivos previstos:** criar `agents/guardrail.py`, `graph/nodes/guardrail.py`,
`repositories/health_reports.py`, `config/prompts/guardrail.md`,
`src/fittrack/db/migrations/versions/_0007_health_report_idempotency.py`,
`tests/unit/test_guardrail.py`, `tests/integration/test_health_report_persistence.py`; modificar
`graph/root.py`, `graph/state.py`, `tests/test_graph_topology.py`. **Condicional:** ADR de topologia
do `HEALTH_REPORT` (ambiguidade #19).

**Migração (`_0007`).** `ALTER TABLE processing_batch ADD CONSTRAINT uq_processing_batch_id_tenant
UNIQUE (id, tenant_id);` seguido de `ALTER TABLE health_report ADD COLUMN batch_id BIGINT, ADD
CONSTRAINT fk_health_report_batch_tenant FOREIGN KEY (batch_id, tenant_id) REFERENCES
processing_batch(id, tenant_id);` e `CREATE UNIQUE INDEX ux_health_report_idempotency ON
health_report(tenant_id, batch_id) WHERE batch_id IS NOT NULL;` — `batch_id` sozinho (sem a
composta) deixaria a policy de RLS validar só a linha própria de `health_report`, não o `batch_id`
referenciado, e um tenant A poderia gravar proveniência apontando para um `processing_batch` de
tenant B (mesma classe de bug que a T12/ADR-0012 já corrigiu para `exercise_set_source` — nenhuma FK
tenant-scoped nesta sprint fica de fora desse padrão). Nullable para não travar uma inserção manual
futura fora do fluxo de batch, mas todo `INSERT` desta tarefa sempre preenche.

**Plano.** Veredito Pydantic fechado (`PASS`, `HEALTH_REPORT`, `MEDICAL_ADVICE`, `EXTREME_DIET`,
`OFF_TOPIC`, `ABUSE`), `extra="forbid"`. Em `HEALTH_REPORT`, o schema devolve
`source_segments: list[int]` não vazia — índices em `NormalizedTurn.segments`, nunca um offset de
caractere: o mesmo problema e a mesma solução da T09 (ADR-0012) — o guardrail também só vê o turno
normalizado, nunca o fragmento bruto. Python resolve `source_segments` via
`domain/provenance.resolve_spans(...)` para os `SourceSpan` que a T08 já calculou, decifra
`text_extract` ou `transcript` conforme `source_field` de cada span e reconstrói **somente** o
relato literal, nunca `clean_text` e nunca uma reprodução escrita pelo próprio LLM.

Três ramos, não dois — a versão anterior deste plano colapsava `HEALTH_REPORT` dentro do mesmo
`Command` que `PASS`, e por isso nunca escrevia o aviso em `outbound`:

- `PASS` → `Command(goto="router")`, sem `update`.
- Categorias bloqueantes (`MEDICAL_ADVICE`, `EXTREME_DIET`, `OFF_TOPIC`, `ABUSE`) →
  `Command(goto="voice", update={"health_flag": …, "outbound": [bloco semântico]})`.
- `HEALTH_REPORT` → `Command(goto="router", update={"health_flag": …, "outbound":
  [{"kind": "health_notice", "region": …}]})`. Vai para `router`, não para `voice` (ADR-0014, §12.1:
  a série do mesmo turno precisa continuar sendo registrada), **mas** ainda assim escreve o bloco
  `health_notice` em `outbound` — a mesma chave com reducer que `ingestion` escreve o `ack` no mesmo
  estágio. Sem isso, uma mensagem mista ("dói o ombro, supino 80x8") persistia a série e o relato e
  respondia só com o `ack`, nunca entregando a orientação profissional que a §12.2 exige; o
  `voice_agent` (T26) só sabe formatar o que está em `outbound`, não sabe que um `health_flag` sozinho
  precisa virar texto — ele nunca lê `health_flag` diretamente, só os blocos (invariante 2: um único
  caminho de saída, e é `outbound`).

`HEALTH_REPORT` grava o relato **antes** de seguir para `router`. A reserva de
`health_report_id_seq` continua existindo — o AAD do `ColumnCipher` precisa do `row_id` para cifrar
`verbatim` **antes** de qualquer `INSERT`, então não há como evitar reservar um candidato primeiro.
**O que muda é que reservar um id deixou de ser, sozinho, a garantia de não-duplicação:** o repositório
insere com `INSERT ... (id, tenant_id, batch_id, region, severity, category, verbatim, key_version)
VALUES (candidate_id, ...) ON CONFLICT (tenant_id, batch_id) WHERE batch_id IS NOT NULL DO NOTHING
RETURNING id` — o predicado repete o de `ux_health_report_idempotency` porque é um índice parcial;
sem repeti-lo o Postgres não o infere como alvo do conflito e o `INSERT` falha. Se o
`RETURNING` vier vazio, a linha já existe — este `batch_id` já foi processado antes, e o checkpoint do
grafo é que não sobreviveu — e o `candidate_id` recém-reservado fica só como gap na sequência
(inofensivo, Postgres não garante sequência sem buracos). O repositório então faz
`SELECT id ... WHERE tenant_id = … AND batch_id = …` e usa **esse** `id` — o da linha que já existe,
cifrada com o AAD correspondente a ela — para o `health_flag` que segue no `update`; nunca o
`candidate_id` descartado. Sem o `ON CONFLICT`, um node que roda de novo por retry inseria uma segunda
linha com um `id` novo e duplicava o relato ativo — o que reaparece depois como dois check-ins de
acompanhamento (§12.2 regra 4) para o mesmo evento. Disclaimer é marcado no estado para não se repetir
a cada mensagem; o texto final do `health_notice` continua sendo decisão de T26.

**Primeiros testes que devem falhar.** `test_medical_advice_skips_router` — `assert result.goto == "voice"`;
`test_health_report_persists_with_batch_idempotency_key`, que decifra o valor somente com o AAD do
tenant/linha original; e `test_health_report_retry_after_uncommitted_checkpoint_does_not_duplicate`,
que insere, simula falha de checkpoint e reprocessa o mesmo `batch_id`, provando `COUNT(*) == 1`.

**Critérios de aceite:** as seis categorias passam, incluindo dor fragmentada normalizada e injeção
delimitada; a integração prova que `health_report.verbatim` é ciphertext `BYTEA` sem o literal, que
decifra com o AAD exato e que tanto RLS quanto a cópia do blob para outra linha/tenant recusam o acesso
ou a autenticação; `test_graph_topology` prova `normalizer → guardrail`, `guardrail → router` e
`guardrail → voice`; o caso `HEALTH_REPORT` prova `goto == "router"`, `health_flag` preenchido **e**
`"health_notice"` presente em `outbound["kind"]`; um teste de mensagem mista (relato de dor + série
válida no mesmo turno) prova que a série persiste **e** `outbound` carrega os dois blocos
(`ack` e `health_notice`) até o `voice`, não só um deles.

**Tamanho:** M.

#### S03-T24 — Desvio de correção no subgrafo de ingestão · **adiada para a Sprint 04**

**Objetivo.** `is_correction=True` desvia de extração para um `correction_agent` estruturado, com
validação determinística antes de atualizar ou apagar linhas já persistidas.

**Spec:** §§8.4, 8.5, 9.2–9.3, 9.10, 13.2, 17.4, 20.2, 22.3. *A §9.7 é o contrato do `analysis_agent`:
não existe seção própria para correção — essa é a origem da ambiguidade #20.*

**Depende de:** S03-T08 (`is_correction`), S03-T12 (persistência), S03-T03 (subgrafo), S03-T21, S03-T26.
**Bloqueada por ADR** — ver ambiguidade #20.

**Arquivos previstos:** criar `agents/correction.py`, `config/prompts/correction.md`,
`tests/unit/test_correction.py`, `tests/integration/test_correction_flow.py`; modificar
`graph/subgraphs/ingestion.py`, o session manager, o repositório de `exercise_set`, `graph/state.py`.

**Plano.** O `session_manager` troca apenas o próximo nó por `Command(goto="correction")` — extração e
resolver não rodam. O agente recebe **só** uma lista limitada de séries recentes já tenant-filtradas,
com ids opacos; Python valida que o alvo pertence ao tenant/sessão e que o patch respeita o schema.
Update/tombstone em transação idempotente. O bloco de correção **força modo texto**, para a pessoa ver
o que mudou mesmo quando a anotação original recebeu só emoji.

**Primeiro teste que deve falhar.** `test_correction_turn_bypasses_extraction` —
`assert calls == ["session_manager", "correction", "persistence"]`.

**Critérios de aceite:** corrigir reps, apagar a última série, alvo inexistente, **linha de outro
tenant** e reprocessamento do mesmo batch sem dupla mutação; `test_graph_topology` mostra a aresta
`session_manager → correction`; ADR aceito antes da primeira linha de mutação.

**Tamanho:** G.

#### S03-T25 — `summary_agent` no fechamento de sessão · **adiada para a Sprint 04**

**Objetivo.** Gerar, persistir e entregar um resumo narrativo de 2–4 frases a partir de métricas
determinísticas, **sem o LLM recalcular número nenhum** (invariante 1).

**Spec:** §§6.1–6.4, 8.4, 8.5, 9.2, 13, 20.2, 22.2–22.3, 24.

**Depende de:** S03-T12, S03-T14, S03-T21, S03-T26.

**Arquivos previstos:** criar `agents/summary.py`, `config/prompts/summary.md`,
`tests/unit/test_summary_agent.py`, `tests/integration/test_session_summary.py`; modificar o subgrafo
de ingestão, o repositório de `session_summary`, `scheduler.py`, `graph/state.py`.

**Plano.** Volume, total de séries, duração, grupos, RPE médio e PRs saem de Python/SQL e vão ao
agente **como JSON delimitado, nunca como instrução**. O schema de resposta só aceita narrativa; antes
de persistir, o código verifica que todo número citado veio do conjunto de métricas. `narrative` é
cifrada com AAD de tenant/linha (invariante 8). Fechamento automático dispara a **mesma** operação
durável do scheduler — nada de chamar LLM dentro de um laço em memória.

**Primeiro teste que deve falhar.**
`test_explicit_close_persists_deterministic_metrics_and_one_semantic_summary_block` —
`assert summary.total_sets == 3`.

**Critérios de aceite:** o teste rejeita uma narrativa que inventa um número;
`test_schema_contract` passa com `session_summary.narrative` cifrado; sessão `closed_auto` usa a mesma
operação idempotente e `discarded` **não** gera resumo.

**Tamanho:** G.

#### S03-T26 — Nó `voice_agent`: a única decisão do que o usuário vê

**Objetivo.** Converter os blocos semânticos acumulados em um único `VoiceOutput`, respeitando persona,
contexto e `ChannelCaps`; escrever **somente** `state.voice_output` e nunca chamar canal ou fila.

**Spec:** §§8.2–8.4, 8.9, 9.2, 9.10, 12.2–12.3, 13.1–13.6, 18.1, 18.4, 20.2, 22.3.

**Depende de:** S03-T21; T23 e T25 enriquecem os blocos, mas o nó tem de renderizar um conjunto vazio
ou degradado desde o primeiro dia. **Bloqueia** a colagem final de T19.

**Arquivos previstos:** criar `graph/nodes/voice.py`, `config/prompts/voice.md`,
`tests/unit/test_voice_node.py`, `tests/evals/test_voice_output.py`; modificar `agents/voice.py`,
`graph/root.py`, `tests/test_channel_isolation.py`.

**Plano.** `graph/nodes/voice.py` é o **único** lugar que lê `state["channel_caps"]` e converte
`ChannelCaps` em restrições explícitas de renderização. A tabela de modo da §13.2 é implementada
com a restrição física do ADR-0005 nesta fase: ack confiante pode reagir; conjunto incompleto ou baixa
confiança **força texto**; clarificação vira botões se couber e lista numerada se não couber;
**progresso com `chart_path` também força `mode="text"`**, verbalizando a leitura determinística que
acompanha o gráfico, porque o único caminho de entrega é durável e não aceita arquivo local; o PNG só
volta quando existir blob storage durável. O nó não emite `mode="media"` nesta sprint. O resto é texto.
`split` é a lista final de bolhas e é proibido em sessão ativa, erro, clarificação e reação. O nó
escreve `{"voice_output": output.model_dump(mode="json")}` e retorna — não importa `OutboundService`,
não resolve identidade, não abre conexão.

**Primeiro teste que deve falhar.**
`test_clarification_degrades_to_numbered_text_when_options_exceed_cap` — `assert output.mode == "text"`.

**Critérios de aceite:** reação, texto, botões, silêncio, baixa confiança e split cobertos; progresso
com `chart_path` produz texto não vazio com a leitura correspondente e **nunca** `mode="media"` nesta
fase; `test_channel_isolation` passa com a lista de exceções **ainda** exatamente
`voice.py`/`deliver.py`;
`rg -n 'enqueue_response|OutboundService|\.send\(' src/fittrack/graph/nodes/voice.py` não encontra
nada; o teste de equivalência prova que Telegram e WhatsApp recebem o mesmo conteúdo, variando só
formato.

**Tamanho:** G.

#### S03-T27 — Onboarding guiado, perfil e consentimentos LGPD

**Objetivo.** Tirar o tenant do estado `onboarding` por uma máquina guiada que coleta perfil mínimo e
consentimentos granulares, registrando hash e versão do texto apresentado antes de liberar registro de
treino ou voz.

**Spec:** §§5.2, 8.2–8.4, 9.2, 11.3, 18.1, 19.1, 19.5, 22.2–22.3, 24.

**Depende de:** S03-T21, S03-T26, S03-T04 (checkpoint/interrupt), S03-T08, S03-T12.

**Arquivos previstos:** criar `agents/onboarding.py`, `services/consent.py`, `repositories/profile.py`,
`config/prompts/onboarding.md`, `tests/unit/test_onboarding.py`,
`tests/integration/test_onboarding_consent.py`; modificar `graph/subgraphs/admin.py`, `graph/root.py`,
`agents/router.py`, `services/stt.py`, `graph/state.py`, `llm/roles.py`.

**Por que não é adiável.** O `bootstrap.py` cria `tenant.state = 'onboarding'` e o STT já exige
consentimento de `workout_data`. Sem esse fluxo o primeiro usuário não tem como consentir, e **toda
nota de voz é recusada para sempre** — o caminho feliz da sprint não fecha.

**Plano.** Rotar por `admin/manage_consent` com subestado guiado e checkpoint, **sem ampliar `Target`**
(ver ambiguidade #23). Uma pergunta por vez, retomada durável. Cada consentimento é uma linha
**imutável** com `kind`, `granted`, hash SHA-256 do texto exato, versão e timestamp; revogação cria
fato novo, não atualiza o anterior. `terms` + `workout_data` tornam o tenant `active`; `health_data`,
`proactive_msg` e `model_training` são opt-ins separados. O bloqueio no `VoiceIngestion` permanece como
defesa em profundidade.

**Primeiro teste que deve falhar.**
`test_workout_data_consent_activates_tenant_and_opens_voice_gate`.

**Critérios de aceite:** hash e versão do texto gravados, revogação e reinício em checkpoint cobertos;
STT e onboarding usam **a mesma** consulta de consentimento; `test_tenant_isolation` passa para perfil
e consentimentos; botão com payload forjado é rejeitado.

**Tamanho:** G.

#### S03-T28 — Aposentar o ADR-0009 e levar recusas pré-grafo pelo caminho único

**Objetivo.** Remover toda decisão e todo enqueue de resposta visível de `VoiceIngestion`/ingress,
transportar apenas **eventos semânticos de recusa** até o nó `voice`, e fazer de `deliver` o único
enfileirador.

**Spec:** §§4, 8.2–8.4, 9.3, 11.3, 13, 17.3–17.4, 18.2, 18.4, 20.2, 22.3; ADR-0005, 0006, 0008, 0009.

**Depende de:** S03-T21, S03-T26, S03-T05 (`process_batch → ainvoke`), S03-T19 (`deliver`), S03-T16
(drain). **É a última mudança funcional da sprint** — não ligar o desvio antes de `voice → deliver`
estar coberto por teste de integração, sob pena de trocar uma violação de invariante por silêncio.

**Arquivos previstos:** criar `tests/integration/test_pregraph_refusals_via_graph.py` e
`doc/adr/0016-recusa-pre-grafo-pelo-voice.md`; modificar `services/stt.py`, `services/batch.py`,
`services/webhook.py`, `worker.py`, `services/outbound.py`, `graph/root.py`, `graph/nodes/voice.py`,
`graph/nodes/deliver.py`, `doc/adr/0009-respostas-fixas-antes-do-grafo.md`, `doc/adr/README.md`.

**Plano.** `VoiceOutcome.reply: OutboundBlock | None` vira um evento interno fechado
(`refusal: Literal["inaudible", "too_long", "no_consent"] | None`); `_refuse()` deixa de ler prompt,
criar bloco, chamar `enqueue_response()` e marcar `answered_at`. O evento entra no batch como fragmento
de controle **sem texto e sem `media_ref`** — não é uma segunda decisão de resposta, é a informação de
que responder é obrigatório. `load_context` detecta uma rajada formada só por eventos de controle e vai
direto ao `voice`, sem normalizer, sem guardrail, sem router e **sem LLM**: não há fala para
normalizar. `voice` mapeia o motivo para a constante versionada da §11.3 e valida o `VoiceOutput`;
`deliver` converte e enfileira. Idempotência por `group_id` derivado de `(raw_message_id, refusal)`,
**dentro do `deliver`**, não no STT.

**Um achado factual que muda o roteiro.** Ao contrário do que a Sprint 02 descreve, o ingress real
**não** chama `enqueue_unsupported_media()`: `TelegramWebhookIngress.accept()` persiste imagem e
documento, e `_is_processable()` os exclui do buffer. O helper existe e tem teste unitário, mas **não
tem caller de produção** — hoje imagem e documento ficam sem resposta nenhuma. A migração não pode
alegar ter removido uma chamada que não existe: o trabalho real é **parar de descartá-los** e
encaminhá-los como evento de controle `unsupported_media`, fechando a resposta obrigatória da §18.4.

**Primeiro teste que deve falhar.** `test_inaudible_voice_reaches_deliver_without_stt_enqueue` —
`assert reply_queue.calls == []`.

**Critérios de aceite:** inaudível, >5 min, falta de `workout_data`, imagem e documento chegam **cada
um a uma única linha** de `outbound_queue`, e só depois de `voice_output`;
`rg -n 'enqueue_response|enqueue_unsupported_media|OutboundBlock' src/fittrack/services/stt.py src/fittrack/services/webhook.py`
não encontra caminho de enqueue; retry idempotente com `group_id` estável e **sem `answered_at` novo**;
ADR-0009 e o índice de ADRs exibem `substituído por ADR-0016`, **sem extensão de prazo**.

**Tamanho:** G.

## Ordem de PRs

Numeração ≠ ordem. As três trilhas avançam em paralelo depois do dia 1.

| # | Branch | Tarefa | Trilha | Paraleliza com |
| --- | --- | --- | --- | --- |
| **0** | `doc/sprint-03-adrs` | ADR-0010 a ADR-0015, ADR-0018 + erratas | — | **antes de qualquer código** |
| 1 | `feat/graph-state` | S03-T01 | A | T06, T10, T18 |
| 1 | `feat/llm-gateway` | S03-T06 | B | T01, T10, T18 |
| 1 | `feat/exercise-catalog` | S03-T10 | B | T01, T06, T18 |
| 1 | `hotfix/revoked-identity-resolution` | S03-T18 | C | tudo — **sem dependência de código** |
| 2 | `feat/graph-topology` | S03-T02 | A | T07, T15, T21 |
| 2 | `feat/llm-fallback-quota` | S03-T07 | B | T02, T15, T21 |
| 2 | `feat/outbound-claim` | S03-T15 | C | T02, T07, T21 |
| 2 | `feat/decision-contracts` | S03-T21 | D | T02, T07, T15 — **destrava C e D** |
| 3 | `feat/graph-subgraphs` | S03-T03 | A | T04, T08, T16 |
| 3 | `feat/graph-checkpointer` | S03-T04 | A | T03 (arquivos disjuntos) |
| 3 | `feat/conversation-normalizer` | S03-T08 | B | T03, T04 |
| 3 | `feat/outbound-drain` | S03-T16 | C | T03, T04 |
| 4 | `feat/extraction-rpe` | S03-T09 | B | T11, T22 |
| 4 | `feat/exercise-resolver` | S03-T11 | B | T09, T22 |
| 4 | `feat/router-staging` | S03-T22 | D | T09, T11 |
| 5 | `feat/set-persistence` | S03-T12 | B | T23, T26 |
| 5 | `feat/guardrail-agent` | S03-T23 | D | T12, T26 |
| 5 | `feat/voice-node` | S03-T26 | D | T12, T23 — **destrava T19** |
| 6 | `feat/deliver-node` | S03-T19 | C | T27 |
| 6 | `feat/onboarding-consent` | S03-T27 | D | T19 |
| 7 | `feat/graph-worker-handoff` | S03-T05 | A | — |
| 8 | `feat/retire-adr-0009` | S03-T28 | D | nada — **fecha a sprint** |

**A onda 0 não é burocracia.** Sete ADRs bloqueiam tarefas concretas: o 0011 bloqueia a T06, os 0013 e
0018 a T09, o 0012 a T08/T09/T12/T23 (o `domain/provenance.py` que a T08 cria e a resolução
determinística de spans que a T09 e a T23 consomem, além da migração que a T12 executa), o 0014 a
T23, o 0015 a T26, e o 0010 as T04 e T15. Escrevê-los depois é escrever a justificativa de uma decisão
que já virou código — que é o oposto de um ADR.

**T18 sobe primeiro.** É bug latente de produção, não tem dependência de código com nada, e é a única
tarefa da sprint que corrige algo que já está acontecendo.

**T21 sobe cedo por ser contrato, não código.** É pequena e destrava três consumidores: `deliver`
(T19), o nó `voice` (T26) e a migração do ADR-0009 (T28). Atrasá-la faz a trilha C escrever contra um
contrato imaginado.

**T28 é a última, e a ordem não é negociável.** Ela desliga o caminho de resposta que existe hoje. Se
subir antes de `voice → deliver` estar coberto por teste de integração, o resultado não é uma
violação de invariante — é silêncio para o usuário, que é pior porque não quebra nada visivelmente.

## Escopo decidido

O planejamento produziu 28 tarefas. **Decisão de 2026-09-04: opção 1 — cortar 6, executar 22.**

| Trilha | Tarefas nesta sprint | Cobre |
| --- | --- | --- |
| A | T01–T05 | trilha inteira — sem o grafo nada existe |
| B | T06–T12 | gateway, normalizer, extração, catálogo, resolver, persistência |
| C | T15, T16, T18, T19 | claim, drain, tenant órfão (#30), o único enfileirador |
| D | T21, T22, T23, T26, T27, T28 | contrato, router, guardrail, voice, onboarding, ADR-0009 |

**Sprint 04 herda 5 tarefas:** T13 (clarification), T14 (manutenção de sessão), T20 (redação PII),
T24 (correction, com o ADR-0017) e T25 (summary) — mais o golden set v1.

**T17 saiu de vez.** Pela [decisão #26](#26-crítico-de-escopo--t17-e-t28-escrevem-e-apagam-o-mesmo-código),
a T28 fecha o item 3 da issue #29 pelo caminho do grafo. Como consequência direta, **a issue #29 só
fecha quando a T28 fechar** — e a T28 é a última PR da sprint. Se ela escorregar, a issue escorrega
junto e o ADR-0009 precisa de prazo novo por escrito.

Duas tarefas foram declaradas não-cortáveis, e a razão é factual, não preferencial:

- **T27 (onboarding/LGPD)** — `bootstrap.py` cria `tenant.state = 'onboarding'` e o STT já exige
  consentimento de `workout_data`. Sem o fluxo, ninguém consente e toda nota de voz é recusada
  indefinidamente.
- **T28 (ADR-0009)** — o ADR expira nesta sprint por prazo próprio.

## Decisões tomadas

Todas as 27 ambiguidades foram decididas em 2026-09-04. Esta tabela é a autoridade; a seção seguinte
preserva o raciocínio que levou a cada uma.

| # | Decisão | Vira |
| --- | --- | --- |
| 1 | Adapters **nativos** (`groq`/`anthropic`); `BaseMessage` só como contrato de entrada | **ADR-0011** |
| 2 | RPE e RIR contraditórios: preservar os dois, marcar baixa confiança, nunca corrigir em silêncio | **ADR-0013** |
| 3 | Separar `IMPORT_EXCEPTIONS` de `CAPS_EXCEPTIONS` no teste de isolamento | decisão na T01 |
| 4 | `current_step` vive no schema privado de cada subgrafo | suposição no PR |
| 5 | A fronteira do checkpoint é `thread_id` + principal dedicado + cifra ligada ao tenant; o Store sempre injeta namespace de tenant | **ADR-0010** |
| 6 | ZSET `interrupts:pending` pontuado pelo deadline | suposição no PR |
| 7 | Proveniência **plural e imutável** por spans validados, com chave canônica de idempotência | **ADR-0012** |
| 8 | Fronteira de manutenção cross-tenant por `SECURITY DEFINER` estreita | **ADR-0010** |
| 9 | Delta de empate próximo no trigram: `0.06` | errata da §10 |
| 10 | `config/prompts/resolver.md` deve existir | errata da §23 |
| 11 | Lease do claim via `next_retry_at`, sem coluna nova | suposição no PR |
| 12 | Role nova `fittrack_outbound` | suposição no PR |
| 13 | `voice_output`: chave nova, escritor único, sem reducer | errata da §8.2 |
| 14 | Os cinco alvos do router existem desde já; dois degradam honestamente | suposição no PR |
| 15 | Catálogo gerado por LLM e **curado pelo mantenedor antes da PR** da T10 | decisão de produto |
| 16 | `answered_at` **não** vira segundo marcador da resposta de mídia | — |
| 17 | ADR-0009 **aposentado**, não estendido | **ADR-0016** |
| 18 | O LLM devolve `steps`; `stage_plan()` é a **única** autoridade sobre estágios | errata da §9.4 |
| 19 | `HEALTH_REPORT → router`, com `health_flag` e aviso acumulado | **ADR-0014** |
| 20 | Correção ganha ADR próprio **e** crítico determinístico | **ADR-0017** (Sprint 04) |
| 21 | *kind* `session_summary` entra em `outbound`, não em `VoiceOutput` | emenda §13.1 (Sprint 04) |
| 22 | Fechamento automático por job durável idempotente | coberto pelo **ADR-0010** |
| 23 | Onboarding por `admin/manage_consent`, **sem ampliar `Target`** | — |
| 24 | `fallback_text` obrigatório em `mode="reaction"`, redigido pelo `voice` | **ADR-0015** |
| 25 | Persistir a narrativa agora; indexar no Qdrant só na fase 1.1 | errata §6.4 |
| 26 | Cortar a T17; a T28 fecha o item 3 da issue #29 | escopo |
| 27 | LLM devolve quantidade literal + unidade; Python converte com `Decimal` e arredondamento fixo | **ADR-0018** |

### Os ADRs exigidos antes da implementação

| ADR | Assunto | Decisões | Bloqueia | Quando |
| --- | --- | --- | --- | --- |
| 0010 | Fronteira de manutenção cross-tenant | #5, #8, #22 | T04, T15 | onda 0 |
| 0011 | SDKs nativos em vez de LangChain | #1 | T06 | onda 0 |
| 0012 | Proveniência plural da série por spans | #7 | T08, T09, T12, T23 | onda 0 (esta PR) |
| 0013 | RPE e RIR contraditórios | #2 | T09 | onda 0 |
| 0014 | `HEALTH_REPORT` segue para o router | #19 | T23 | onda 0 |
| 0015 | `fallback_text` obrigatório em reação | #24 | T26 | onda 0 |
| 0016 | Recusa pré-grafo pelo `voice` — substitui o ADR-0009 | #17 | — | com a T28 |
| 0017 | Contrato de correção | #20 | T24 | **Sprint 04** |
| 0018 | Conversão determinística de unidades fora do LLM | #27 | T09 | onda 0 (esta PR) |

O ADR-0014 é o único de **segurança** da lista: ele decide que um relato de dor não faz o usuário
perder o registro do treino. Escrever a justificação com cuidado importa mais nele do que nos outros.

### As erratas da spec

Uma única PR `doc/spec-errata-sprint-03` corrige, todas com a mesma justificativa ("a spec se
contradiz ou omite; a decisão está registrada"): §5.2/§9.5 (proveniência plural e `source_text`),
§9.5 (quantidade literal e conversão determinística), §9.4 (`steps` vs. `stages`), §8.2
(`voice_output`, `destination_identity_id`, reset dos acumuladores e janela de 12 mensagens), §5.3/§8.4
(serializer cifrado e Store com namespace injetado), §10 (delta `0.06`), §23 (`resolver.md`) e
§6.4/§24 (indexação do resumo). A emenda da §13.1 (*kind* `session_summary`) fica para a Sprint 04,
junto da T25 que a consome.

## A fronteira de manutenção cross-tenant

Ler as três fatias juntas expôs um padrão que nenhuma delas via sozinha: **quatro tarefas
independentes descobriram, cada uma por seu caminho, que precisam atravessar tenants e que a RLS
corretamente as impede.**

| Tarefa | O que precisa varrer | Achado |
| --- | --- | --- |
| S03-T15 | `outbound_queue` global | sessão sem `app.tenant_id` vê zero linhas |
| S03-T18 | `channel_identity` incluindo revogadas | `resolve_tenant_for_identity` filtra `revoked_at IS NULL` |
| S03-T14 | `workout_session` de todos os tenants | scheduler não pode usar repository sem tenant |
| S03-T04 | tabelas do LangGraph | `SET LOCAL app.tenant_id` vive na conexão SQLAlchemy; o saver tem pool psycopg próprio |

Resolver quatro vezes é resolver errado uma vez. **Decidido (#5, #8, #22): um único
[ADR-0010](#os-adrs-exigidos-antes-da-implementação), "fronteira de manutenção cross-tenant"**, que fixa o padrão comum
sem confundir os dois principais. O **runtime** e o principal dedicado do saver permanecem
`NOSUPERUSER NOBYPASSRLS`. Para varrer uma tabela de domínio sob `FORCE ROW LEVEL SECURITY`, a porta é
uma função `SECURITY DEFINER` estreita, com `search_path` fixo, sem SQL arbitrário e retornando o
mínimo; seu dono é uma role separada `NOLOGIN NOSUPERUSER BYPASSRLS`, sem membros e com os menores
grants de tabela/coluna/sequence possíveis, no padrão efetivo da `fittrack_identity` da `_0002`. O
runtime recebe apenas `EXECUTE` na função e **não** herda a role dona. Sem o `BYPASSRLS` do dono, a
função global continua sujeita à policy, não tem `app.tenant_id` e vê zero linhas. Já as tabelas do
LangGraph usam o segundo padrão do mesmo ADR: `thread_id` construído pelo código, principal de login
`fittrack_graph NOBYPASSRLS` com DML limitado às tabelas operacionais e **sem** poder sobre tabelas de
domínio, `TenantEncryptedSaver` + `EncryptedSerializer` ligados ao tenant/thread e
`TenantScopedStore` que injeta o namespace antes de toda operação e cifra o valor. Ali não existe RLS
porque o schema é da biblioteca; isolamento lógico, menor privilégio e confidencialidade contra dump
são três camadas distintas e testadas. O grant é obrigatoriamente pós-`setup()` no bootstrap — nunca
Alembic tentando referenciar tabela inexistente.
As quatro tarefas instanciam a seção correspondente. A fronteira do checkpointer (#5) e o fechamento
automático de sessão (#22) são seções dele, não ADRs próprios.

**Nesta sprint o ADR-0010 é instanciado diretamente por duas tarefas** — T04 usa o padrão do saver
`NOBYPASSRLS`; T15 usa a função com dono `BYPASSRLS` e runtime apenas com `EXECUTE`. A T18 estende a
fronteira pré-tenant já aceita na §19.1 e implementada pela `_0002`, portanto não espera o ADR-0010.
Na Sprint 04, T14 e o fechamento automático da T25 **reusam a porta `SECURITY DEFINER`**; é exatamente
para elas não inventarem um segundo mecanismo que o ADR é escrito agora.

**O anti-padrão a proibir explicitamente no ADR:** usar o DSN de owner, dar `BYPASSRLS` ao runtime ou
torná-lo membro da role dona das funções. O `BYPASSRLS` existe somente no dono `NOLOGIN`, sem membros,
e só é alcançável pelas funções com superfície mínima. Um teste de claim que "funciona" contra
conexão de superuser é o erro mais caro possível aqui — as policies existem e nunca são avaliadas.

## Ambiguidades — registro e leitura recomendada

**Todas foram decididas** — a tabela de [Decisões tomadas](#decisões-tomadas) é a autoridade. Esta
seção fica para preservar *por que* cada decisão foi tomada, que é o que um ADR precisa citar e o que
qualquer um vai querer reler daqui a seis meses. Onde se lê "leitura recomendada", leia-se "decisão
aceita": o operador acompanhou a recomendação em todos os 26 casos.

Nenhuma destas era decisão de implementador. As marcadas **crítico** bloqueavam a tarefa
correspondente.

#### 1. SDK nativo vs. LangChain na camada de provider
A §7.4 cita `ChatGroq` e `ChatAnthropic`, mas o `pyproject.toml` instala os SDKs **nativos** `groq` e
`anthropic` — não `langchain-groq` nem `langchain-anthropic`. **Leitura recomendada:** adapters
nativos, preservando `BaseMessage` de `langchain-core` apenas como contrato de entrada. Acrescentar os
pacotes LangChain exige justificar dependência nova. **Exige ADR** — é divergência declarada da spec.
Bloqueia T06.

#### 2. RPE e RIR explícitos e contraditórios
A spec não cobre o caso. **Leitura recomendada:** preservar os dois valores informados, **não
"corrigir" em silêncio**, marcar baixa confiança, e pedir esclarecimento só se inviabilizar o registro.
Introduzir regra automática de precedência **exige ADR**.

#### 3. `channel_caps` no `GraphState` reprova o teste de isolamento hoje
Verificado executando o checker do repositório: `reads_channel_caps` dispara em
`ast.Name(id="channel_caps")`, que é exatamente o alvo de uma anotação de campo num `TypedDict` — e
`test_the_exceptions_are_named_and_few` fixa `EXCEPTIONS` em dois arquivos. **Leitura recomendada:**
separar `IMPORT_EXCEPTIONS` de `CAPS_EXCEPTIONS`, porque a §8.2 escopa a regra de capacidades a
"qualquer módulo sob `graph/subgraphs/`" — a implementação atual é **mais estrita que a spec**, e
declarar o campo não é lê-lo. A proibição de **import** de `channels/` continua sobre `graph/` e
`agents/` inteiros. Não exige ADR; exige decisão explícita na T01, com asserção substituta.

#### 4. `current_step` não existe no `GraphState` da §8.2
O `dispatch` da §8.4 faz `Send(step["target"], {**state, "current_step": step})`, mas a §8.2 não
declara o campo. Lacuna real. **Leitura recomendada:** vive no `input_schema` privado de cada
subgrafo — no pai ele viajaria em todo checkpoint e criaria campo com dois escritores potenciais.

#### 5. RLS não alcança as tabelas do LangGraph
Uma policy ingênua (`thread_id = 'tenant:' || current_setting(...)`) bloquearia **todo** acesso ao
checkpoint, não só o cruzado, porque o saver tem pool psycopg próprio sem gancho para `SET LOCAL`.
**Leitura recomendada:** a fronteira tem quatro peças inseparáveis: `thread_id` derivado pelo código;
principal `fittrack_graph` com DML restrito às tabelas operacionais; `TenantEncryptedSaver` que força
todo valor de estado pelo `EncryptedSerializer` ligado ao tenant; e `TenantScopedStore` que prefixa
cada namespace e cifra seus valores. O Store bruto nunca chega ao grafo. **Exige ADR** — ver
[fronteira de manutenção](#a-fronteira-de-manutenção-cross-tenant).
Alternativas recusadas: GUC fixada no DSN, incompatível com pool; confiar só no `thread_id`, que não
escopa `AsyncPostgresStore`; e persistir texto decifrado em blobs legíveis num dump.

#### 6. Um `interrupt` expirado no Redis não é varrível
A §8.7 diz "grava-se `interrupt:{tenant_id}` com TTL de 20 min. O scheduler varre expirados a cada
minuto" — mas uma chave expirada **deixou de existir**. **Leitura recomendada:** ZSET
`interrupts:pending` pontuado pelo deadline (`ZRANGEBYSCORE 0 now` devolve os expirados) **mais** a
chave da §17.1 para o lookup barato. Notificações de expiração do Redis não servem: não têm entrega
garantida. Suposição registrada no PR, sem ADR.

#### 7. **crítico** — uma série pode vir de vários fragmentos, mas `source_message_id` é escalar
Nada define qual mensagem representa `"supino"` / `"80"` / `"8 reps"` ditas em três fragmentos.
Escolher arbitrariamente a primeira ou a última — ou concatenar `clean_text` — **perde a
auditabilidade que a §9.3/§9.5 exigem**. O **ADR-0012** fixa `source_spans` ordenados e validados contra
os fragmentos brutos como prova canônica; `source_text` só pode ser o recorte literal de um único span
ou `NULL`, nunca evidência sintética. A T12 persiste a relação imutável de cada span com o
`raw_message` tenant-qualificado e deriva a chave de idempotência do conjunto canônico de spans.

#### 8. **crítico** — o scheduler precisa varrer todos os tenants, e a RLS corretamente o impede
Não existe hoje fronteira de manutenção autorizada. **Exige ADR antes da implementação** da T14 — ver
[fronteira de manutenção](#a-fronteira-de-manutenção-cross-tenant). **Não usar o DSN de owner no
scheduler.**

#### 9. O delta de "empate próximo" da camada trigram
O §10 não define. **Leitura recomendada:** `0.06`, igual ao gap vetorial. Registrar em ADR ou
explicitar na spec.

#### 10. `config/prompts/resolver.md` não está na §23
A §23 enumera os prompts e omite o do resolver, embora o §10 exija desempate por LLM. Pela convenção
obrigatória de prompts versionados, o arquivo **deve existir**. Divergência menor da spec; registrar
no PR.

#### 11. Lease via `next_retry_at` vs. coluna `claimed_at`
A §18.4 não define o mecanismo de atomicidade além do `SKIP LOCKED`. **Leitura recomendada:** lease via
`next_retry_at`, reusando a semântica de elegibilidade existente, sem coluna nova. **Alternativa
descartada:** manter a transação aberta durante o envio — prenderia conexão do pool durante HTTP +
espera do rate limiter, inviável com 10 jobs concorrentes por worker.

#### 12. Role nova `fittrack_outbound` vs. reusar `fittrack_identity`
**Leitura recomendada:** role nova. O docstring da `_0002` trata o blast radius da role como a razão de
sua existência, e drenar fila não é resolver identidade.

#### 13. ~~Onde a saída do `voice_agent` mora no `GraphState`~~ — **resolvida**
A §8.2 declara `outbound` como **entrada** do voice e não declarava a saída dele. A trilha D confirmou
a proposta: chave `voice_output`, escritor único, **sem reducer**, guardando
`model_dump(mode="json")`. Contrato completo em [S03-T21](#s03-t21--contratos-de-decisão-voiceoutput-e-extensão-do-graphstate).
Segue sendo lacuna da §8.2 — vale errata, não ADR.

#### 14. Os cinco alvos do router existem na fase 1.0?
A §24/fase 1.0 lista só `ingestion` no caminho do grafo, mas a §9.4 fecha o vocabulário do router em
cinco rótulos, e **o critério de saída da fase 1.0 é "acurácia de roteamento ≥ 0,95"** — que não se
mede sobre três rótulos se o gabarito tem cinco. **Leitura recomendada:** os cinco nós existem desde
já; `analysis` e `recommendation` são degradações honestas ("ainda não sei fazer isso") em vez de
rotas inexistentes. Suposição no PR, sem ADR.

#### 15. Fonte canônica e formato do catálogo global
A §24 pede "~300 exercícios" sem definir fonte nem formato. **Decidido:**
`data/exercises/global_catalog.json` versionado, **gerado por LLM e curado pelo mantenedor antes da
PR** da T10. A geração é rascunho, não entrega: o catálogo global é lido por todos os tenants e um
slug errado aí vira alias errado em toda a base. A T10 não abre PR antes do sinal verde da curadoria.
Os slugs seguem o AD-27: pt-BR sem acento (`supino_reto_barra`).

#### 16. `answered_at` como segundo marcador da resposta de mídia
**Leitura recomendada: não.** O `group_id` derivado já dá idempotência, e `answered_at` é "escrito
apenas por `mark_answered`" do caminho de voz (ADR-0008); estender o significado aqui recriaria o
acoplamento que o ADR-0008 corrigiu.

#### 17. O ADR-0009 expira nesta sprint — **decisão tomada: aposentar**
As quatro fatias convergiram nisso independentemente. A trilha D fechou: **aposentar, não estender.**
A situação prevista pelo ADR chegou — haverá `voice_agent` e `deliver` — e a própria condição de
revisão dele já determina que `VoiceIngestion` passe a devolver só status. Estender autorizaria por
mais tempo dois responsáveis por decidir resposta, sem fato novo que justifique.

O transporte correto não é texto vazio a ser adivinhado por um LLM: é um **evento de controle fechado**
(`inaudible`, `too_long`, `no_consent`, `unsupported_media`) que diz que responder é obrigatório, mas
não carrega frase, emoji, formato nem destino. Execução em [S03-T28](#s03-t28--aposentar-o-adr-0009-e-levar-recusas-pré-grafo-pelo-caminho-único);
ADR-0016 substitui o ADR-0009. **Nunca deixar expirar em silêncio.**

#### 18. **crítico** — o router devolve estágios ou passos?
`RoutingPlan.stages` na §9.4 diz que o LLM devolve **estágios**; a regra seguinte da mesma seção diz que
o LLM propõe **passos** e que `stage_plan()` cria os estágios. A spec se contradiz. **Leitura
recomendada:** schema bruto `steps: list[RouteStep]`, com `stage_plan()` como **única** autoridade
sobre `list[PlanStage]` — é a única forma de provar escrita antes de leitura sem confiar no LLM.
Errata de spec basta se for lapso do autor; **ADR** se alguém defender a leitura contrária, por tocar o
AD-15. Bloqueia T22.

#### 19. **crítico** — `HEALTH_REPORT` vai a `voice` ou a `router`?
O exemplo da §8.4 manda **qualquer** não-`PASS` direto a `voice`; a §12.1 exige registrar a série mesmo
quando há relato de dor — o que obriga a prosseguir até a ingestão. As duas não podem estar certas.
**Leitura recomendada:** `HEALTH_REPORT → router` com `health_flag` e aviso acumulado; as demais
categorias bloqueantes vão a `voice`. **Exige ADR de segurança/topologia antes de codificar** — muda
como uma decisão de saúde é aplicada, e a invariante 6 ("falhar registrando") está do lado do router.
Bloqueia T23.

#### 20. **crítico** — correção não tem contrato nenhum
As §§8.5/9.2 citam correção mas não definem identificação da série, operações permitidas, forma do
patch, múltiplas correções, janela temporal nem conflito com ack por emoji. *(A §9.7 é o contrato do
`analysis_agent` — a referência cruzada que circulava está errada.)* Deixar o LLM escolher por texto
livre uma linha de banco é exatamente o que a invariante 3 proíbe. **Exige ADR próprio, com crítico
determinístico**, antes da primeira linha de mutação. Bloqueia T24.

#### 21. Falta o bloco semântico de resumo na enumeração da §13.1
A §6.4 exige entregar o resumo de sessão, mas a enumeração de blocos da §13.1 não inclui
`session_summary`. **Leitura recomendada:** acrescentar o *kind* ao contrato de `outbound` (**não** ao
`VoiceOutput`) e tratá-lo como texto com split possível no `voice`. É contrato intersubgrafo: emenda
explícita de spec ou ADR antes da T25.

#### 22. O fechamento automático de sessão não tem entrada de grafo definida
A §6.1 atribui o fechamento ao scheduler; a §8.3 parte sempre de um batch de mensagem. **Leitura
recomendada:** job durável que cria um evento de sistema identificado por `session_id`, passa pelo
mesmo resumo/persistência e é idempotente. **Exige ADR se o scheduler invocar o root graph fora de
`processing_batch`.** Relacionado à ambiguidade #8.

#### 23. Como o onboarding entra no vocabulário fechado do router
`Target` tem cinco valores e os intents de `admin` não incluem `onboard`, mas a §24 exige onboarding na
fase 1.0. **Leitura recomendada:** rotar tenant em onboarding por `admin/manage_consent` com subestado
guiado — **sem ampliar `Target`**, porque ampliá-lo mexe em dispatch, plano, métricas e no gabarito de
roteamento que a fase 1.0 mede em ≥ 0,95. Se o produto quiser um alvo `onboarding` de verdade, isso é
ADR.

#### 24. Reação inválida não tem texto de fallback
A §13.1 diz que `text` é para `mode="text"` (ou legenda), mas §13.2/§18.4 exigem que o adaptador degrade
para texto uma reação recusada pelo Telegram ou sem mensagem-alvo. Com o schema literal, o adaptador
não tem texto — e **inventá-lo violaria a invariante 2**. **Leitura recomendada:** campo
`fallback_text`, obrigatório em `mode="reaction"`, redigido pelo `voice`; o adaptador só pode usá-lo
após falha de protocolo, nunca redigir. **ADR curto** — altera contrato inter-fatia. Até lá, emitir
somente os emojis garantidos pelo mapa da §13.2 e tratar reação inválida como bug.

#### 25. Indexação do resumo no Qdrant
A §6.4 a descreve no fechamento da sessão; a §24 só a lista na fase 1.1. **Leitura recomendada:**
persistir a narrativa nesta sprint e adiar a indexação — o roadmap é a ordenação explícita de entrega.
Confirmação editorial na spec basta; sem ADR.

#### 26. **crítico de escopo** — T17 e T28 escrevem e apagam o mesmo código
A T17 (trilha C) liga um `UnsupportedMediaReplier` que **enfileira no ingress**, fora do grafo, sob a
autorização temporária do ADR-0009. A T28 (trilha D) desliga esse caminho e leva imagem e documento ao
`voice` como evento de controle. Fazer as duas na mesma sprint é pagar duas vezes pelo mesmo
comportamento. **Leitura recomendada:** cortar a T17 e deixar a T28 fechar o item 3 da issue #29
diretamente — *desde que* a T28 caiba na sprint. Se a T28 escorregar para a Sprint 04, a T17 volta como
ponte, e o ADR-0009 precisa de prazo novo por escrito. **Decidido: cortar a T17.**

> **Fato que corrige a descrição da Sprint 02:** o ingress **não** chama `enqueue_unsupported_media()`
> hoje. `TelegramWebhookIngress.accept()` persiste imagem e documento e `_is_processable()` os exclui
> do buffer; o helper existe, tem teste unitário e **nenhum caller de produção**. Qualquer das duas
> tarefas está criando comportamento novo, não migrando comportamento existente.

#### 27. **crítico** — a §9.5 manda o LLM converter unidades
`load_kg` e a regra do prompt "libras/lbs → converte" fazem o modelo calcular e arredondar um valor
que será armazenado e analisado depois. Isso contradiz diretamente a invariante 1: o LLM classifica o
literal, não faz aritmética. O **ADR-0018** fixa `QuantityLiteral(value, unit)` na saída crua e a
normalização em `domain/units.py`, com `Decimal("0.45359237")` para lb → kg, `Decimal("1000")` para
km → m, `Decimal("60")` para min → s (a mesma lacuna existia para `duration_s`/`hold_s`/`rest_s`) e
`ROUND_HALF_UP` no limite de persistência. Bloqueia T09 e exige errata da §9.5.

## Critério de saída da sprint

- [ ] `"Supino reto com 10 kg, 8 repetições e foi fácil"` vira uma linha em `exercise_set` com
  `load=10.0`, `reps=8`, `rpe=4`, sessão aberta e `status='complete'`;
- [ ] `3x10` vira **três** linhas com `set_index` 1, 2, 3 (AD-07);
- [ ] o usuário **recebe** a confirmação no Telegram — enqueue → claim → send → `sent_at`;
- [ ] foto/documento recebem resposta, nota de voz recusada recebe resposta, e **ambas chegam**;
- [ ] reentrega de evento de bloqueio não cunha tenant órfão (issue #30);
- [ ] nada além de `deliver` escreve em `outbound_queue`; nada além de `voice_agent` decide saída;
- [ ] `make test-architecture` roda **três** arquivos — `test_channel_isolation`, `test_graph_reducers`
  e `test_graph_topology` — e passa;
- [ ] `thread_id` é construído em exatamente um lugar, a partir do código;
- [ ] `fittrack_graph` escreve e retoma checkpoint cifrado, não lê domínio, e os grants só são aplicados
  pelo bootstrap depois de `setup()`; a poda deixa 1 por thread;
- [ ] matar o worker no meio do grafo e reprocessar retoma do último super-step;
- [ ] duas rajadas consecutivas no mesmo thread não repetem `outbound`, `errors`, séries extraídas nem
  IDs persistidos da anterior; `messages` nunca passa de 12 e o digest avança a cada 20 interações;
- [ ] toda operação do Store recebe namespace injetado pelo código; dois tenants com a mesma key não
  se enxergam e o Store bruto não é exposto ao grafo;
- [ ] nenhum nome de modelo em Python; nenhum prompt embutido em string;
- [ ] `external_id`, `external_id_hash`, `file_path` e `media_ref` provados ausentes **do estado
  serializado no checkpoint** (trilha A). *A prova equivalente para log e span do Datadog é a T20, que
  foi adiada — ver [Riscos](#riscos-e-mitigação);*
- [ ] texto de usuário/turno/saída provado ausente em claro de `checkpoint_blobs`,
  `checkpoint_writes` e `store`; mover um blob entre tenants falha autenticação;
- [ ] exercício privado de outro tenant não é lido, sugerido nem persistido;
- [ ] um relato de dor no mesmo turno de um registro de treino **grava a série** e recebe o aviso
  (§12.1 + invariante 6);
- [ ] um tenant novo conclui o onboarding, `terms` e `workout_data` ficam gravados com hash e versão
  do texto, e só então a primeira nota de voz é aceita;
- [ ] recusa de STT e mídia não suportada chegam ao usuário **passando pelo `voice`** — nenhum enqueue
  em `services/stt.py` ou `services/webhook.py`;
- [ ] **ADR-0010 a ADR-0015 e ADR-0018 escritos e mergeados antes da primeira PR de código** — a onda 0;
- [ ] a PR de erratas da spec mergeada (§§9.4, 8.2, 10, 23, 6.4/24);
- [ ] ADR-0009 resolvido pelo ADR-0016 — substituído, nunca vencido em silêncio;
- [ ] `make fmt/lint/typecheck/test` e `make test-integration` verdes; CI obrigatório verde.

## Riscos e mitigação

| Risco | Impacto | Mitigação nesta sprint |
| --- | --- | --- |
| `graph/state.py` reprova `test_channel_isolation` na primeira PR | Médio — parece bug do teste | T01 resolve deliberadamente (ambiguidade #3) |
| `defer=True` num grafo com laço (`join → dispatch → ramos → join`) | **Crítico** — `join` que dispara cedo avança o cursor com ramo correndo; intermitente e proporcional à carga | Teste na T02 com plano de 2 estágios afirmando **exatamente** 2 rodadas de `dispatch` e ordem escrita→leitura |
| `recursion_limit=40` é mais apertado do que parece | Alto — ingestão + clarificação + 2 estágios chega a 20–25 super-steps | Teste na T02 medindo o caminho mais profundo e afirmando margem ≥ 40% |
| `durability` mudou de grafia dentro do pin `>=0.6,<0.8` | **Crítico** — kwarg errado não levanta erro, degrada para assíncrono em silêncio, e o super-step perdido pode ser o `persistence` de uma série | Teste de **comportamento** (ler checkpoint entre super-steps), nunca asserção sobre o nome do argumento |
| Pool psycopg sem `autocommit=True, row_factory=dict_row` | Alto — falha só na primeira gravação, em produção | T04 com teste de integração que grava de fato |
| Grant do LangGraph em Alembic | **Crítico** — num banco limpo a migração referencia tabelas que só o `setup()` seguinte criaria e o bootstrap para antes de criá-las | Grant exclusivamente no passo pós-`setup()` do bootstrap; integração parte de banco vazio e roda duas vezes |
| Saver sem envelope de primitivos/serializer ou Store bruto exposto | **Crítico** — conteúdo decifrado reaparece nos blobs/JSONB (o saver 2.0.25 embute primitivos antes do serializer) e um namespace omitido cruza tenants | T04 inspeciona todas as colunas como owner, testa digest escalar e falha de autenticação cross-tenant e entrega ao grafo somente wrappers ligados ao tenant |
| Lease expirando com o envio em voo | Médio — bolha duplicada | Lease generoso (300s) > pior espera do limiter + timeout HTTP, documentado com teste; a §17.4 já assume at-least-once |
| Teste de claim rodando como superuser/owner | **Crítico** — policies existem e nunca são avaliadas; "funciona" e não protege | Testes de claim rodam como `fittrack_runtime` e **provam o negativo** |
| `try/except` largo do subgrafo engolindo `NameError` | Alto — bug vira "não consegui agora" para sempre | Registrar tipo e traceback em log estruturado; alerta separa `TransientLLMError` do resto |
| `checkpoint_blobs` domina o banco | Médio — guarda o estado inteiro cifrado por super-step, e o `GraphState` carrega `raw_fragments` + `messages` + `outbound` | Janela de 12 mensagens + poda diária na T04 + métrica `graph_checkpoint_bytes` como acompanhamento |
| Determinismo de LLM em teste | Médio — suíte instável | `temperature=0` reduz variância mas não a elimina: unitários usam providers falsos; golden set mede ao vivo, separado |
| Custo do judge em toda PR da trilha A | Baixo — o filtro do CI inclui `src/fittrack/graph/` | Não mudar o filtro; **saber**: contar o custo no encerramento e não confundir "judge não calibrado" com regressão |
| Colisão de numeração de migração `_0006`/`_0007` | Baixo — T15, T08 e T12 propõem `_0006`; T18 e T23 propõem `_0007` | Resolver no merge: primeira a mergear fica com o número, as demais renumeiam o próprio arquivo (nome e `down_revision`) |
| Trilha D chegar tarde e bloquear T19 | Alto — `deliver` sem `VoiceOutput` não integra | **Mitigado:** T21 é só contrato e sobe na onda 2. T19 continua escrita contra estado fake; a colagem no grafo é PR separada |
| T28 subir antes de `voice → deliver` estar coberto | **Crítico** — desliga o único caminho de resposta que existe hoje e o substituto não está provado; o sintoma é **silêncio**, não erro | T28 é a última PR da sprint, com teste de integração dos cinco motivos de recusa antes do merge |
| Cortar T27 por parecer "produto" | **Crítico** — sem onboarding ninguém consente `workout_data`, e toda nota de voz é recusada para sempre | **Resolvido:** T27 declarada não-cortável no escopo decidido |
| T17 e T28 na mesma sprint | Médio — código escrito e apagado no mesmo ciclo | **Resolvido:** decisão #26 cortou a T17 |
| **A sprint inteira roda sem a T20** | Alto — fica sem o teste que impede `external_id` e `file_path` (que carrega o token do bot na própria URL) de vazarem para o Datadog. É uma sprint inteira de código novo de observabilidade **sem a rede** | Aceito conscientemente no corte. **T20 é a primeira tarefa da Sprint 04.** Até lá: nenhum atributo de span novo com texto, revisado à mão em cada PR que tocar `observability/` |
| A issue #29 depender da última PR da sprint | Médio — a T28 fecha o item 3; se escorregar, a issue escorrega | Explicitado no escopo decidido; se a T28 cair para a Sprint 04, o ADR-0009 ganha prazo novo **por escrito**, nunca por omissão |
| `voice_output` ganhar um segundo escritor "só desta vez" | Alto — é a invariante 2 se dissolvendo por conveniência | Chave declarada **sem** reducer: um segundo escritor concorrente levanta `InvalidUpdateError` em vez de passar despercebido |

## Suposições registradas

- Os cinco alvos do router existem desde esta sprint; `analysis` e `recommendation` degradam
  honestamente em vez de não existirem (ambiguidade #14).
- `current_step` vive no schema privado de cada subgrafo, não no `GraphState` do pai.
- O worker monta `channel_caps` e resolve `destination_identity_id`, `origin_channel` e `reply_to` a
  partir do último `raw_message_id`; o nó `load_context` carrega só domínio — é o que mantém o AD-39 e
  o `load_context.py` limpos. `reply_to` nunca é usado para descobrir a identidade.
- Os quatro acumuladores com reducer recebem `BatchReset` somente quando o checkpoint anterior tem
  outro `batch_id`; retry/resume do batch corrente preserva o estado.
- `messages` usa reducer limitado a 12; o digest a cada 20 interações é reconstruído das referências
  canônicas cifradas, não de uma janela de checkpoint que cresça até 20.
- `TenantEncryptedSaver` força inclusive primitivos pelo serializer cifrado ligado ao tenant/thread;
  todo acesso ao Store passa por wrapper que injeta namespace e cifra valores; os objetos brutos não
  são dependência de nó.
- O lease do claim reusa `next_retry_at`; nenhuma coluna nova.
- `group_id` derivado do `raw_message_id` + motivo é o padrão de idempotência para toda resposta em
  caminho reentregável. Um grupo aleatório fresco nesse caminho é bug.
- A instrumentação Langfuse/Datadog fica fora; esta sprint entrega o *seam* e a lista de redação.
- `voice_output` guarda `dict` serializado, não instância Pydantic, e **todo consumidor revalida** —
  ninguém confia num `dict` vindo do checkpoint.
- O onboarding é rotado por `admin/manage_consent`; `Target` continua com cinco valores
  (ambiguidade #23).
- O catálogo global é gerado por LLM e **curado à mão** antes de virar PR (ambiguidade #15). Nenhum
  slug entra sem revisão humana.
- A sprint roda sem a T20: a rede de redação PII só chega na Sprint 04, e até lá a defesa é revisão
  manual de cada PR que tocar observabilidade.
- Consentimento é fato imutável: revogação cria linha nova, nunca atualiza a anterior.
- O desvio `load_context → voice` para rajadas formadas só por eventos de controle **não** é uma segunda
  entrada de linguagem natural — não há fala para normalizar. Precisa constar do ADR-0016 e do teste de
  topologia.
- `raw_message.answered_at` para de receber escritas novas na T28, mas **não é removido** enquanto
  houver drenos legados recuperáveis dentro da retenção de 90 dias.
- Os relatórios de planejamento das **quatro** fatias estão preservados em `.polly/planning/`
  (diretório **não rastreado** — considerar adicioná-lo ao `.gitignore`).

## Relatório de encerramento

Ao concluir a sprint, registrar neste documento:

- PRs mergeados por tarefa;
- ADRs escritos — esperados ADR-0010 a ADR-0016 e ADR-0018 (o ADR-0017 fica para a Sprint 04, com a T24);
- estado da PR de erratas da spec;
- suposições efetivamente usadas e quais se mostraram erradas;
- itens adiados e motivo;
- estado de cada item do critério de saída;
- custo acumulado das rodadas de judge disparadas pelo filtro de `src/fittrack/graph/`;
- riscos novos para a sprint seguinte.
