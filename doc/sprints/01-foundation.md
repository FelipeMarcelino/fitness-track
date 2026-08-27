# Sprint 01 — Executable Foundation

| Campo | Valor |
| --- | --- |
| Fase | 1.0 — Registro confiável |
| Duração | 2 semanas |
| Estado | `planned` |
| Objetivo | Tornar o repositório executável, testável e seguro para receber os fluxos de Telegram e LLM nas sprints seguintes |
| Referências principais | spec §§3.1, 5.2, 19.1, 21.4, 22.2, 23 e 24 |

## Resultado esperado

Ao final da sprint, um novo clone consegue entrar no devshell, instalar dependências, executar os
quality gates e subir a infraestrutura local. O banco nasce por migração com o schema da §5.2,
campos sensíveis já cifrados e Row Level Security testada para todas as tabelas tenant-scoped.

Esta sprint constrói a fundação da Fase 1.0; ela não conclui a fase. O critério de saída da fase
continua sendo o da §24: 20 usuários reais por duas semanas, acurácia de extração ≥ 0,90,
roteamento ≥ 0,95 e nenhum vazamento entre tenants.

## Escopo

Incluído:

- pacote Python e comandos padronizados de desenvolvimento;
- serviços locais de infraestrutura;
- configuração tipada e tratamento de secrets;
- schema inicial e migrações Alembic;
- criptografia de coluna, hash pesquisável de identidade e rotação por versão;
- isolamento por tenant em repositório e RLS;
- quality gates e testes de arquitetura aplicáveis à fundação;
- bootstrap e documentação para reproduzir o ambiente.

Fora do escopo:

- `TelegramAdapter`, webhook, polling, voz e envio de mensagens;
- debounce, filas ARQ funcionais e processamento de rajadas;
- `LLMGateway`, providers, prompts e chamadas de LLM;
- grafo LangGraph e agentes;
- catálogo de exercícios, Qdrant funcional e golden set de produto; entra apenas a baseline do
  judge exigida antes da primeira PR de código;
- Langfuse/Datadog instrumentados na aplicação;
- deploy, DNS, TLS público e credenciais reais.

Os containers podem existir nesta sprint sem integrações de produto. Não serão criadas
implementações fictícias de Telegram, LLM ou grafo apenas para preencher a árvore da §23.

## Princípios de execução

1. Cada tarefa vira um PR independente, salvo quando duas tarefas forem explicitamente agrupadas.
2. A ordem abaixo é a ordem padrão de merge; tarefas marcadas como paralelas podem avançar juntas.
3. Todo comportamento começa por um teste que falha pelo motivo esperado.
4. Teste `skip` ou `xfail` não satisfaz critério de aceite.
5. Imagens, actions e dependências são fixadas por versão; dependências de orquestração respeitam
   os limites definidos na spec.
6. Nenhum secret ou payload sensível aparece em log, fixture versionada ou mensagem de erro.

## Dependências

```text
S01-T01 Project toolchain
   ├── S01-T02 Local infrastructure ──┐
   └── S01-T03 Typed configuration ───┼── S01-T04 Initial database
                                     │          │
                                     │          ├── S01-T05 Column encryption
                                     │          └── S01-T06 Tenant isolation
                                     │                     │
                                     └─────────────────────┴── S01-T07 Integrated validation
```

T02 e T03 podem ser implementadas em paralelo depois de T01. T05 e T06 podem ser implementadas em
paralelo depois de T04, desde que não alterem a mesma migração simultaneamente.

## Tarefas

### S01-T01 — Project toolchain and quality gates

**Objetivo.** Criar o pacote instalável, o contrato de dependências e uma única interface de
comandos para desenvolvimento e CI.

**Spec.** §§8 (pin de LangGraph), 21.4, 23 e 24.

**Depende de:** nada.

**Arquivos previstos:**

- `.envrc`;
- `pyproject.toml` e lock do `uv`;
- `Makefile`;
- `src/fittrack/__init__.py`;
- `tests/unit/` e `tests/integration/`;
- `evals/run_judge.py`, `evals/rubrics/` e datasets de calibração/baseline;
- `.github/workflows/ci.yml`;
- ajustes em `flake.nix`, `shell.nix`, `.gitignore` e `CLAUDE.md` quando necessários.

**Plano de implementação:**

1. Criar um teste de importação de `fittrack` e vê-lo falhar porque o pacote ainda não existe.
2. Confirmar Python 3.13 em `doc/spec.md`, Nix e metadados do pacote antes de fixar
   `requires-python`; qualquer divergência remanescente bloqueia o PR.
3. Configurar Ruff, mypy e pytest em `pyproject.toml`.
4. Criar os alvos `fmt`, `lint`, `typecheck` e `test`; cada alvo deve falhar se sua ferramenta
   falhar.
5. Antes de introduzir código de aplicação, implementar o runner do judge, 20 casos de calibração
   com nota humana e uma baseline de 40 respostas sintéticas cobrindo segurança e fidelidade
   numérica. A calibração segue a política da §21.2; os agentes reais substituem os casos sintéticos
   nas sprints em que forem implementados.
6. Adicionar `make eval-judge` e o job condicional por paths da §21.4. Segurança ou fidelidade
   numérica abaixo de 5 bloqueiam; judge não calibrado é reportado sem reprovar a PR.
7. Fazer o CI chamar os mesmos alvos do `Makefile`, sem duplicar comandos.
8. Atualizar a tabela “Estado atual” do `CLAUDE.md` removendo apenas itens realmente entregues.

**Critérios de aceite:**

- `direnv allow .` seguido de `uv sync` cria/atualiza `./.venv` sem baixar outro Python;
- `make fmt`, `make lint`, `make typecheck` e `make test` terminam com sucesso;
- `pytest` descobre a suíte e o teste de importação passa;
- o job `Quality` do CI usa o `Makefile` e fica verde;
- o judge acerta pelo menos 18 dos 20 casos de calibração e avalia a baseline de 40 casos;
- os testes do runner provam os gates bloqueantes sem depender de uma resposta ao vivo;
- nenhum artefato de ambiente ou secret entra no Git.

**Validação:**

```bash
direnv allow .
uv sync
make fmt
make lint
make typecheck
make test
make eval-judge
```

### S01-T02 — Local infrastructure

**Objetivo.** Disponibilizar a topologia local necessária para desenvolvimento e testes de
integração, sem expor bancos publicamente.

**Spec.** §§3.1, 15.3, 17 e 22.1.

**Depende de:** S01-T01.

**Arquivos previstos:**

- `Dockerfile`;
- `docker-compose.yml` e `docker-compose.dev.yml`;
- `Caddyfile`;
- `.env.example`;
- `.github/workflows/ci.yml`;
- healthchecks e scripts mínimos de inicialização.

**Plano de implementação:**

1. Escrever um teste/validador da configuração que falhe enquanto os serviços obrigatórios não
   existirem.
2. Adicionar `postgres`, `redis`, `qdrant`, `langfuse` e `caddy` com imagens versionadas,
   healthchecks, volumes e rede interna.
3. Adicionar `ingress`, `worker` e `scheduler` usando a mesma imagem da aplicação, inicialmente com
   entrypoints mínimos de health/smoke; nenhum deles implementa comportamento de negócio.
4. Publicar somente Caddy no compose de produção; portas de desenvolvimento ficam exclusivamente
   no override local.
5. Configurar CA/certificados internos e TLS verificado para Postgres (`sslmode=verify-full`), Redis
   e Qdrant. Certificados de desenvolvimento são gerados para teste e nunca reutilizados em
   produção.
6. Criar em T02 o job de CI com Postgres, Redis e Qdrant saudáveis, antes de qualquer PR de banco.
   O job executa a marca de integração separadamente e preserva logs dos serviços em caso de falha.
7. Garantir ordem por healthcheck, encerramento previsível e persistência dos volumes.

**Critérios de aceite:**

- `docker compose -f docker-compose.yml -f docker-compose.dev.yml config --quiet` valida a
  configuração combinada;
- o compose combinado deixa todos os serviços previstos saudáveis;
- Postgres, Redis e Qdrant não publicam portas no compose de produção;
- clientes da aplicação verificam a CA e conexões plaintext ou com certificado inválido falham;
- o worker executa a suíte usando explicitamente os dois arquivos Compose;
- o job de integração do CI está verde e é obrigatório antes de iniciar T04;
- `.env.example` contém apenas nomes e valores seguros de exemplo.

**Validação:**

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --wait
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm worker pytest
docker compose -f docker-compose.yml -f docker-compose.dev.yml down
```

### S01-T03 — Typed configuration and secret boundaries

**Objetivo.** Centralizar configuração em `pydantic-settings`, validar o ambiente no boot e impedir
que secrets sejam serializados ou logados.

**Spec.** §§7.2, 15.3, 19.3, 22 e Apêndice A.

**Depende de:** S01-T01. Pode ocorrer em paralelo com S01-T02.

**Arquivos previstos:**

- `src/fittrack/settings.py`;
- `config/models.yaml`, `config/quota.yaml` e `config/rag.yaml`;
- `tests/unit/test_settings.py`;
- `.env.example`.

**Plano de implementação:**

1. Testar falha clara para variável obrigatória ausente, valor inválido e canal sem credenciais.
2. Definir settings imutáveis por processo e tipos explícitos para URLs, limites e modos.
3. Usar tipos secretos para chaves e tokens, com representação redigida.
4. Carregar YAMLs versionados separadamente de secrets e validar referências entre eles.
5. Testar que `repr`, erros de validação e logs não contêm valores sensíveis.

**Critérios de aceite:**

- boot inválido falha antes de abrir conexão externa;
- settings válidos carregam de ambiente e arquivos de configuração;
- secrets nunca aparecem em `repr`, serialização ou logs capturados;
- nomes de modelos existem apenas em `config/models.yaml`;
- testes cobrem defaults, overrides e falhas de validação.

**Contrato criptográfico.** Validar `FITTRACK_ENCRYPTION_KEYS` como keyring versionado cujas versões
estão no intervalo compartilhado `1..32767` (`SMALLINT` positivo e dois bytes no blob),
`FITTRACK_ACTIVE_KEY_VERSION` como uma versão do mesmo intervalo existente no keyring e
`FITTRACK_IDENTITY_PEPPER` como segredo independente. Nenhum dos três pode aparecer em logs ou
erros de validação.

### S01-T04 — Initial database schema

**Objetivo.** Materializar o schema da §5.2 desde a primeira migração, incluindo constraints,
índices, roles e separação entre dono de migração e usuário da aplicação.

**Spec.** §§5.2, 5.3, 19.1 e 22.2.

**Depende de:** S01-T02 e S01-T03.

**Arquivos previstos:**

- `alembic.ini`;
- `src/fittrack/db/engine.py`;
- `src/fittrack/db/migrations/env.py`;
- `src/fittrack/db/migrations/versions/*_initial_schema.py`;
- `tests/integration/test_migrations.py`;
- `tests/integration/test_schema_contract.py`.

**Plano de implementação:**

1. Criar testes de integração que falhem pela ausência das tabelas, constraints e roles.
2. Configurar Alembic assíncrono sem incluir tabelas internas do LangGraph nas migrações.
3. Implementar o schema completo da §5.2, preservando `BYTEA` e `key_version` desde a criação.
4. Criar a role `fittrack_app` como `NOSUPERUSER NOBYPASSRLS`; migrações usam role proprietária
   separada. Provisionar `fittrack_runtime` como principal `LOGIN` não privilegiado e membro apenas
   de `fittrack_app`; a senha vem de secret fora da migração.
5. Vincular `raw_message` a `channel_identity` e deduplicar por
   `(identity_id, channel_message_id)`, com FK composta que também exige o mesmo tenant e canal;
   cobrir IDs iguais em identidades diferentes e rejeitar escopo inconsistente.
6. Manter `processing_batch.combined_text` e `outbound_queue.payload` como `BYTEA` versionado desde
   a primeira migração.
7. Testar upgrade em banco vazio, downgrade somente no banco descartável de teste e novo upgrade.
8. Comparar tabelas, colunas, FKs, checks e índices relevantes com um contrato parametrizado.

**Critérios de aceite:**

- `alembic upgrade head` funciona em Postgres vazio;
- `alembic current` aponta para uma única head;
- a aplicação e os testes conectam como `fittrack_runtime`, nunca como superusuário, role
  `BYPASSRLS` ou dona das tabelas;
- campos da §22.2 são `BYTEA` desde a migração inicial;
- duas identidades podem persistir o mesmo `channel_message_id` sem colisão, mas uma repetição na
  mesma identidade é deduplicada;
- tabelas do checkpointer/store são criadas pelo bootstrap do LangGraph, não pelo Alembic;
- testes de schema e ciclo de migração passam em container limpo.

### S01-T05 — Column encryption and identity lookup

**Objetivo.** Implementar a fronteira criptográfica da aplicação antes de existir qualquer dado de
usuário.

**Spec.** §§1.3 e 22.2.

**Depende de:** S01-T04.

**Arquivos previstos:**

- `src/fittrack/security/crypto.py`;
- `src/fittrack/security/identity_hash.py`;
- `tests/unit/test_crypto.py`;
- `tests/unit/test_identity_hash.py`;
- testes de integração dos campos cifrados.

**Plano de implementação:**

1. Testar vetores de round-trip, adulteração, versão desconhecida e divergência de versão.
2. Implementar AES-256-GCM no formato `version || nonce || ciphertext+tag` definido na §22.2.
3. Validar todas as chaves base64 de 32 bytes do keyring e a versão ativa, mantendo material
   criptográfico fora de logs e erros; rejeitar versões fora de `1..32767` antes do boot.
4. Implementar HMAC-SHA256 determinístico para `external_id_hash`, com pepper separado do banco.
5. Testar nonces aleatórios: plaintexts iguais geram blobs diferentes, mas o hash pesquisável é
   estável.
6. Construir AAD canônico com tenant, tabela, coluna e identidade estável da linha; reservar o ID
   antes de cifrar. Para o primeiro `channel_identity`, usar o contrato pré-tenant da §22.2 baseado
   apenas em canal e `external_id_hash`, permitindo cifrar antes da criação atômica do tenant.
7. Testar substituição de blob íntegro entre linhas, tenants e colunas; todas devem falhar
   autenticação.
8. Testar leitura simultânea de blobs em versões antiga e nova enquanto novas escritas usam apenas
   a versão ativa.
9. Testar que uma chave antiga não pode ser removida enquanto o banco ainda contém sua versão, e
   que a remoção é permitida depois do backfill completo.
10. Implementar e testar a rotação atômica do pepper: ingress pausado, tabela bloqueada, decifra
    com AAD antigo, rehash e recriptografia com AAD novo na mesma transação, troca do secret depois
    do commit e rollback seguro em falha.
11. Testar escrita/leitura cifrada sem permitir consulta ou agregação sobre ciphertext.

**Critérios de aceite:**

- adulterar qualquer byte do blob causa falha fechada;
- versão interna e `key_version` divergentes causam erro explícito;
- versões zero, negativas ou acima de 32767 são rejeitadas na configuração;
- ciphertexts não revelam plaintext e variam por nonce;
- mover ciphertext íntegro para outro contexto falha por AAD;
- rotação progressiva mantém legíveis todas as versões ainda presentes no banco;
- rotação de pepper preserva todos os lookups e não cria tenant duplicado;
- `external_id_hash` permite lookup sem armazenar identificador em claro;
- nenhum teste, exceção ou log expõe chave, pepper ou identificador externo.

### S01-T06 — Tenant isolation

**Objetivo.** Tornar vazamento cross-tenant impossível por contrato de repositório e por RLS no
Postgres.

**Spec.** §§1.3, 5.2, 19.1 e 22.

**Depende de:** S01-T04. Pode ocorrer em paralelo com S01-T05.

**Arquivos previstos:**

- `src/fittrack/repositories/base.py` e primeiros repositórios de suporte;
- fronteira de bootstrap em `src/fittrack/services/identity.py`;
- migração/policies de RLS;
- `tests/test_tenant_isolation.py`;
- `tests/integration/test_identity_bootstrap.py`;
- `tests/integration/test_repository_tenant_context.py`.

**Plano de implementação:**

1. Criar um teste parametrizado que demonstre leitura cruzada antes das policies.
2. Habilitar e forçar RLS na tabela raiz `tenant` (`id = app.tenant_id`) e em todas as tabelas
   tenant-scoped listadas na §19.1.
3. Aplicar `SET LOCAL app.tenant_id` no início de cada transação da aplicação.
4. Exigir `tenant_id` no construtor/contexto dos repositórios de domínio; não oferecer método sem
   escopo.
5. Implementar a fronteira pré-tenant da §19.1 com funções `SECURITY DEFINER` de lookup e criação
   atômica, `search_path` fixo, role dona `NOLOGIN BYPASSRLS`, `PUBLIC` sem `EXECUTE` e apenas
   `fittrack_app` autorizada a chamar as funções.
6. Conceder à role dona `USAGE` no schema, somente `SELECT` em `channel_identity`, `INSERT` em
   `tenant` e `channel_identity` e `USAGE` nas sequences correspondentes; testar ausência de
   qualquer grant adicional.
7. Adicionar policies de leitura para linhas globais somente nas tabelas autorizadas pela spec.
8. Testar leitura, escrita, update e delete cross-tenant conectando como `fittrack_runtime`, que
   herda somente a role de privilégios `fittrack_app`.
9. Testar lookup existente, primeiro contato, identidade revogada, colisão concorrente e que a
   aplicação não consegue consultar `channel_identity` diretamente sem tenant.
10. Testar que conexão sem tenant não retorna silenciosamente dados privados.
11. Testar que linhas globais são legíveis, mas `INSERT`, `UPDATE` e `DELETE` globais falham com ou
    sem `app.tenant_id`; policies de domínio exigem tenant não nulo no `WITH CHECK`.

**Critérios de aceite:**

- o teste percorre `tenant` e todas as tabelas tenant-scoped, não uma amostra;
- tenant A não lê nem altera dados de tenant B;
- o ingress resolve ou cria identidade somente pela fronteira pré-tenant autorizada;
- catálogo global autorizado é legível e não gravável pela aplicação;
- nova tabela tenant-scoped sem policy faz o teste falhar;
- a suíte prova que a conexão usa `fittrack_runtime` com `NOSUPERUSER NOBYPASSRLS` e privilégios
  herdados somente de `fittrack_app`.

### S01-T07 — Architecture guardrails and integrated bootstrap

**Objetivo.** Fechar a sprint com um caminho reproduzível de zero a ambiente validado e impedir que
a futura implementação coloque dependências nas camadas erradas.

**Spec.** §§21.4, 23 e 24; AD-39.

**Depende de:** S01-T02, S01-T03, S01-T04, S01-T05 e S01-T06.

**Arquivos previstos:**

- `scripts/bootstrap.py`;
- `tests/test_channel_isolation.py`;
- testes de smoke/integrados do ambiente;
- `.github/workflows/ci.yml`;
- `README.md` e atualização do `CLAUDE.md`.

**Plano de implementação:**

1. Criar smoke test que falhe enquanto um clone limpo não conseguir migrar e validar serviços.
2. Criar a estrutura mínima de pacotes e o teste AST de isolamento de canal, já bloqueante no CI.
3. Não criar versões vazias de `test_graph_reducers` ou `test_graph_topology`: eles entram no mesmo
   PR que introduzir `GraphState` e o grafo raiz, para não passarem de forma vacuosa.
4. Implementar bootstrap idempotente para migrações e setup explícito das tabelas internas do
   LangGraph quando a dependência estiver disponível; nenhuma chamada de Telegram ou seed faz parte
   desta sprint.
5. Consolidar a ordem dos jobs baratos, unitários e de integração criada em T02; serviços usam
   healthchecks.
6. Documentar setup, comandos, troubleshooting e teardown.
7. Executar a suíte completa duas vezes, sendo a segunda sobre ambiente já inicializado, para
   demonstrar idempotência.

**Critérios de aceite:**

- um clone limpo segue a documentação sem passos implícitos;
- bootstrap pode rodar duas vezes sem duplicar ou destruir dados;
- testes unitários e de arquitetura rodam antes dos testes com containers;
- CI bloqueia em lint, mypy, testes, isolamento de canal e isolamento de tenant;
- todos os serviços necessários ficam saudáveis e a migração chega a `head`;
- a tabela “Estado atual” do `CLAUDE.md` fica vazia e é removida, ou lista somente lacunas reais.

## Ordem de PRs

| Ordem | Branch sugerida | Tarefa | Pode paralelizar |
| --- | --- | --- | --- |
| 1 | `feat/project-toolchain` | S01-T01 | Não |
| 2 | `feat/local-infrastructure` | S01-T02 | Com T03 |
| 3 | `feat/typed-settings` | S01-T03 | Com T02 |
| 4 | `feat/initial-database` | S01-T04 | Não |
| 5 | `feat/column-encryption` | S01-T05 | Com T06, após coordenar a migração |
| 6 | `feat/tenant-isolation` | S01-T06 | Com T05, após coordenar a migração |
| 7 | `feat/integrated-bootstrap` | S01-T07 | Não |

Se um PR revelar que uma tarefa não cabe em revisão segura, ele pode ser dividido sem mudar o
escopo. A divisão deve preservar testes e deixar cada commit/PR em estado executável.

## Critério de saída da sprint

A sprint termina somente quando todos os itens abaixo forem demonstrados:

- [ ] novo clone entra no devshell e instala dependências;
- [ ] `make fmt`, `make lint`, `make typecheck` e `make test` passam;
- [ ] compose de produção não publica bancos e o override local é funcional;
- [ ] ambiente sobe saudável e testes rodam dentro do worker;
- [ ] migração completa chega a uma única `head` em Postgres vazio;
- [ ] campos sensíveis nascem cifrados e a identidade é pesquisada por HMAC;
- [ ] teste parametrizado comprova RLS em `tenant` e todas as tabelas tenant-scoped;
- [ ] bootstrap é idempotente;
- [ ] CI obrigatório está verde;
- [ ] documentação e tabela de estado do `CLAUDE.md` refletem o repositório real.

## Riscos e mitigação

| Risco | Impacto | Mitigação nesta sprint |
| --- | --- | --- |
| Schema amplo exceder a sprint | Alto | Implementar mecanicamente a §5.2, sem reabrir decisões nem criar repositórios de produto |
| RLS existir mas ser ignorada | Crítico | Conectar como `fittrack_runtime`, testar `FORCE RLS` e confirmar `NOBYPASSRLS` |
| Cifra ser adicionada depois | Crítico | Campos já nascem `BYTEA`; T05 bloqueia qualquer fixture sensível em claro |
| Compose local divergir de produção | Médio | Base única com override somente para portas e ergonomia local |
| CI caro ou lento cedo demais | Médio | Guardrails baratos primeiro; integração em job separado com cache |
| Teste arquitetural passar sem arquitetura | Alto | Não criar testes vacuamente verdes para grafo ainda inexistente |

## Suposições registradas

- A sprint dura duas semanas, mas não usa estimativas em pontos até existir velocidade observada.
- Postgres, Redis, Qdrant, Langfuse e Caddy entram como infraestrutura; integração de produto com
  Qdrant e Langfuse fica para sprints posteriores.
- `ingress`, `worker` e `scheduler` têm apenas entrypoints mínimos necessários a health/smoke.
- O schema integral da §5.2 é criado agora para que criptografia e isolamento não virem retrofit.
- O contrato criptográfico usa `FITTRACK_ENCRYPTION_KEYS`, `FITTRACK_ACTIVE_KEY_VERSION` e
  `FITTRACK_IDENTITY_PEPPER`; T03 apenas o valida, não reabre a decisão.

## Relatório de encerramento

Ao concluir a sprint, registrar neste documento:

- PRs mergeados por tarefa;
- comandos e checks executados;
- suposições efetivamente usadas;
- itens adiados e motivo;
- estado de cada item do critério de saída;
- riscos novos que precisam entrar na Sprint 02 ou em ADR.
