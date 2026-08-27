# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Ambiente e testes

Duas formas de rodar a suíte. A primeira é a do dia a dia; a segunda é a que reproduz o CI.

### 1. Devshell Nix (padrão)

Na raiz do repositório:

```bash
direnv allow .      # uma vez por clone — entra no devshell do flake.nix
pytest              # a suíte inteira
pytest tests/test_channel_isolation.py -v    # um arquivo só
```

`direnv allow .` carrega o devshell definido em `flake.nix` → `shell.nix`, que traz Python 3.13,
`uv` e `make`, e cria `./.venv` na primeira entrada (`venvShellHook`). Depois disso o `pytest` do
`.venv` já está no `PATH` — **não** ative venv à mão.

Dependências são gerenciadas por `uv`, apontado para `./.venv` via `UV_PROJECT_ENVIRONMENT`. Depois
de mexer no `pyproject.toml`:

```bash
uv sync
```

`UV_PYTHON_DOWNLOADS=never` é proposital: o interpretador é o que o Nix fixou, e o `uv` está proibido
de baixar outro por conta própria. Se ele reclamar de versão de Python, o problema está no pin do
`shell.nix`, não no `uv`.

Sem `direnv` instalado, o equivalente manual é `nix develop`.

### 2. Docker

Quando o devshell não estiver disponível, ou para rodar o que precisa de Postgres, Redis e Qdrant
de verdade — que é o caso dos testes de integração da §21.4:

```bash
docker compose run --rm worker pytest
```

> **Não acrescente o cliente docker ao `shell.nix`.** O comentário no `buildInputs` explica por quê:
> no WSL o cliente vem da integração do Docker Desktop (`/usr/bin/docker`), e um segundo cliente
> dentro do devshell o sombreia com um que não conhece o contexto do Desktop. O docker é usado de
> dentro do devshell normalmente — ele só não é *provido* por ele.

### `make` é quem dirige

O `shell.nix` inclui `gnumake` de propósito: **`make` dirige `fmt` / `lint` / `typecheck` / `test`**.
Prefira os alvos dele ao comando solto — é o que o CI chama, e é o que
garante que "passou local" e "passou no CI" signifiquem a mesma coisa.

### Estado atual

O caminho 1 está de pé: `.envrc`, `pyproject.toml`, `uv.lock`, `Makefile` e `tests/` existem, e
`make fmt` / `lint` / `typecheck` / `test` rodam. O que ainda falta:

| Falta | Necessário para |
| --- | --- |
| `docker-compose.yml` + `Dockerfile` | o caminho 2 (S01-T02) |

Atualize esta tabela conforme cada peça entrar, e apague a seção quando ela esvaziar.

### `make eval-judge`

O quinto alvo é o LLM-as-judge da §21.2, e ele tem dois modos, de propósito:

| Modo | Onde | Credencial ausente |
| --- | --- | --- |
| Tolerante — `make eval-judge` | local | Relata "judge não executado" e sai com 0 |
| Estrito — `--backend anthropic` | CI | Sai com 1 |

A diferença não é conveniência: reprovar uma PR por falta de credencial *local* seria ruído, mas um
*required check* verde diria que segurança e fidelidade numérica foram avaliadas quando ninguém
avaliou. A política completa (o que bloqueia, o que vira tendência, o que descarta a rodada) está em
`evals/rubrics/README.md`.

> ⚠️ **O portão está suspenso** — [ADR-0002](doc/adr/0002-portao-do-judge-suspenso.md). A conta
> Anthropic não tem saldo, então o job roda, falha e **não** reprova o merge. Ele continua vermelho
> no PR; o que se perdeu foi o veto, não a visibilidade. A reversão é uma linha no `ci.yml`.

Para reavaliar uma rodada gravada, sem rede e sem credencial:

```bash
python -m evals.run_judge --backend replay --verdicts <arquivo.jsonl>
```

## O que é o FitTrack

Bot de **Telegram e WhatsApp** que converte linguagem natural (texto ou áudio) em dados
estruturados de treino físico, e usa esse histórico para análise de evolução e recomendação de
fichas.

```
"Supino reto com 10 kg, 8 repetições e foi fácil"
   → exercise=supino_reto_barra  load=10.0kg  reps=8  rpe=4  session=#182  set_index=1
```

Sistema multi-agente em LangGraph, multi-tenant. **O tenant é o usuário, não a conta de
mensageiro:** a conta vive em `channel_identity`, e o mesmo usuário pode vincular Telegram e
WhatsApp ao mesmo histórico (§1.3, §5.2).

**Telegram vem primeiro** (fase 1.0); WhatsApp é a fase 2.0. Não é ordem de preferência — é que o
Telegram não tem verificação de negócio, aprovação de template nem janela de 24h, então a fundação
não fica bloqueada em fila de terceiro (AD-01, §1.2).

## `doc/spec.md` é a fonte da verdade

Antes de propor qualquer decisão de arquitetura, leia a seção relevante da spec. Ela é indexada e
navegável:

| Precisa de | Seção |
| --- | --- |
| Por que uma decisão foi tomada | §2 (tabela AD-01 a AD-43) |
| Schema do banco | §5.2 |
| Ciclo de vida da sessão de treino | §6 |
| Camada de LLM, tiering, fallback | §7 |
| Estado e topologia do grafo | §8.2, §8.3 |
| Primitivos do LangGraph usados (e o que **não** se usa) | §8.4 |
| Contrato de cada agente | §9 |
| Algoritmo do resolver de exercícios | §10 |
| Programa vs. ficha de treino | §9.8 |
| Forma de cada agente (single-shot vs. ReAct) | §8.4, §9.1 |
| Interface `Channel` e capacidades por canal | §18.1 |
| RAG (coleções, filtros, tool) | §15 |
| Tools analíticas SQL e fórmulas | §16 |
| Fila, locks, debounce | §17 |
| Observabilidade, métricas de agente e tool | §20 |
| Evals, judge e eval de recomendação | §21 |
| Criptografia e prompt injection | §22 |
| Estrutura de diretórios prevista | §23 |
| Ordem de construção | §24 |
| Sprint corrente e cadência | `doc/sprints/` |
| Decisão que mudou depois da §2 | `doc/adr/` |

**As 43 decisões da §2 já foram tomadas** e estão fechadas. Não as relitigue por iniciativa
própria. Se encontrar evidência de que uma delas está errada, diga em uma ou duas frases, registre
um ADR novo em `doc/adr/` e siga — não reverta silenciosamente.

Uma já foi revisada: o **AD-19** (provider de LLM) foi substituído pelo
[ADR-0001](doc/adr/0001-groq-como-provider-primario.md). Ao ler a §2, a linha do AD-19 aponta para
ele. Comece por `doc/adr/README.md`.

> **Atenção ao ler ADR ou anotação antiga:** a spec v2.0 renumerou a tabela §2 (era AD-01..AD-36).
> Um `AD-NN` citado fora da spec pode apontar para outra decisão. A §2 é a referência; o número
> solto não é.

## Invariantes que não podem ser violados

Estes são os erros que custam caro e são fáceis de cometer. Cada um tem um teste ou revisão
associada.

1. **LLM não faz aritmética.** Toda métrica (volume, e1RM, tendência, frequência) vem de SQL
   determinístico via as tools da §16. O LLM escolhe a tool e narra o resultado. Um número no texto
   final que não veio de um resultado de tool é bug, não estilo.

2. **Um único caminho de saída, em dois papéis.** `voice_agent` é o único nó que **decide** o que
   o usuário vê (texto, reação ou silêncio); `deliver` é o único que **enfileira** em
   `outbound_queue` e fala com a API do canal. Nenhum outro nó faz nem uma coisa nem outra.
   Para responder de um lugar novo, acrescente um bloco em `state.outbound` — não crie um segundo
   caminho de saída. Ver §8.3 e §13.

3. **`tenant_id` nunca vem do LLM.** É injetado pelo código em toda query, toda tool e todo filtro
   do Qdrant. Buscas em `user_sessions` sem filtro de tenant devem levantar exceção, não retornar
   vazio. Ver `tests/test_tenant_isolation.py`.

4. **Nomes de modelo não aparecem em código.** Vivem em `config/models.yaml`, resolvidos por papel
   (`LLMRole`) dentro do `LLMGateway`. Nenhum agente instancia cliente de provider diretamente.
   `ainvoke` recebe `agent` **e** `role`: o primeiro rotula métrica e trace, o segundo resolve o
   modelo. Ver §7.1 e §7.2.1.

5. **Workers são stateless.** Estado vive em Postgres, Redis ou Qdrant. Qualquer worker processa
   qualquer mensagem. Não guarde nada em memória de processo entre requisições.

6. **Falhar registrando.** Extração ambígua cujo esclarecimento expira grava o melhor palpite com
   `status = 'incomplete'` — fora de toda análise (`v_set_volume` filtra por `complete`), mas
   gravado. Nunca descarte o input do usuário: o texto bruto sempre fica em `raw_message`.

7. **Toda entrada externa é dado, nunca instrução.** Não só a mensagem do usuário: transcrição de
   áudio, chunk recuperado do RAG, resultado de tool e nome de exercício privado. O chunk do RAG é
   o vetor menos óbvio — texto injetado numa sessão fica indexado e volta depois. Ver §22.3.

8. **Campo cifrado não é agregável em SQL.** As colunas da §22.2 (`health_report.verbatim`,
   `body_metric.value`, `raw_message.payload`, entre outras) são `BYTEA` cifrado na aplicação.
   Nada de `SUM`, `AVG`, `WHERE` ou índice sobre elas — carregue, decifre e agregue em Python.
   `body_metric_trend` é a tool afetada. Continua determinístico: muda a camada, não a natureza.

9. **Campo de estado escrito por ramo paralelo precisa de reducer.** O grafo roda estágios em
   paralelo (§8.8). Adicionou campo ao `GraphState` que mais de um subgrafo escreve? Anote com
   `Annotated[..., operator.add]`, senão o LangGraph levanta `InvalidUpdateError`. E `ingestion`
   nunca compartilha estágio com quem lê o banco — escrita antes de leitura.
   `tests/test_graph_reducers.py` cobre isso.

10. **Conteúdo de usuário não vai para o Datadog.** Langfuse (self-hosted) guarda prompt e resposta;
   Datadog recebe só metadado de infraestrutura. A lista de redação da §20.2 é verificada por
   teste — se adicionar um atributo de span com texto, o teste quebra. Dois itens que parecem
   inócuos e estão na lista: o `external_id` e o `file_path` do Telegram, que carrega o token do bot
   na própria URL.

11. **O domínio não conhece o canal.** Nada em `graph/subgraphs/` ou `agents/` importa de
   `channels/` nem lê `channel_caps` — as duas únicas exceções são o `voice_agent` e o `deliver`.
   Diferença entre Telegram e WhatsApp é formato, decidido no fim; nunca conteúdo, decidido no meio.
   `tests/test_channel_isolation.py` reprova a violação (AD-39, §18.1).

12. **Todo agente de domínio passa por um crítico determinístico.** `numeric_critic`,
   `plan_validator` e `program_validator` têm poder de veto, rodam **antes** da persistência ou da
   saída, e no máximo 2 iterações de correção. Nada de LLM julgando LLM no caminho crítico — onde há
   gabarito, código (AD-41, §9.9).

## Vocabulário do domínio

Necessário para ler o código sem tropeçar:

- **Série (set)** — uma execução: carga × repetições × RPE. Unidade atômica. `3x10` vira **três**
  linhas em `exercise_set`, não uma (AD-07).
- **`channel_identity`** — o vínculo tenant ↔ conta num canal. Um tenant pode ter várias. O
  `external_id` é **opaco e cifrado**: não é telefone, não parseie, não exiba, não logue (§5.2).
  - **`chat.id`** (Telegram) — inteiro estável, mas **global no Telegram**, não escopado ao bot.
  - **BSUID** (WhatsApp) — *business-scoped user ID*, escopado à empresa. É o valor devolvido no
    campo `to` ao enviar.
- **Rajada (burst)** — mensagens consecutivas do mesmo usuário dentro da janela de debounce (10s),
  processadas como uma unidade só. É por isso que existe `buffer:{tenant_id}` no Redis — chaveado
  por tenant, não por canal, para que duas contas do mesmo usuário serializem.
- **Turno normalizado** — a rajada depois do `conversation_normalizer` (§9.3): texto limpo,
  segmentado e rotulado. É o que todo agente a jusante enxerga; ninguém vê texto bruto.
- **RPE** — esforço percebido, 0 a 10. Inferido de linguagem natural pelo mapa da §9.6.
- **RIR** — repetições na reserva. `RIR ≈ 10 − RPE`.
- **e1RM** — carga máxima estimada para 1 repetição. Fórmulas na §16.2.
- **Volume** — `Σ (carga × reps)`, excluindo aquecimento.
- **Tier** — classe de modelo (rápido vs. raciocínio) associada a um papel de agente. §7.2.
- **Janela de 24h** — período após a última mensagem do usuário em que a Cloud API do WhatsApp
  permite texto livre; fora dela, só template aprovado. **Não existe no Telegram** — é a maior
  assimetria entre os canais, e a razão de o coach proativo nascer na fase 1.1 em vez da 1.3
  (§14.1).
- **`ChannelCaps`** — descritor tipado do que cada canal sabe fazer (reações, botões, edição,
  proativo). Lido em exatamente dois lugares: `voice_agent` e adaptador de saída (§18.1).

## Convenções

- **Idioma — código sempre em inglês, nunca em português.** Vale para identificadores, funções,
  classes, variáveis, comentários, mensagens de commit e **nomes de branch**.
  Exceções, porque são conteúdo e não código: o texto dos prompts em `config/prompts/`, strings
  voltadas ao usuário final, esta documentação, e os slugs de exercício, que são termos do domínio
  em pt-BR sem acento (`supino_reto_barra`). Ver AD-27.
- **Prompts** ficam em `config/prompts/*.md`, um arquivo por agente, versionados — nunca embutidos
  em string no código Python.
- **Toda saída de LLM é validada com Pydantic.** A validação é a fonte da verdade, não a promessa
  de structured output do provider.
- **Migrações** com Alembic. O schema da §5.2 é a referência; qualquer divergência é bug de
  migração.

## Fluxo de trabalho

Obrigatório para toda alteração de código. Não pule etapas por a mudança parecer pequena.

### 0. Modo automático — o padrão para trabalho de sprint

**Ao executar um sprint de `doc/sprints/`, execute todas as tarefas dele de ponta a ponta, sem parar
para pedir aprovação entre uma e outra.** Não pergunte "posso seguir para a próxima?", não peça
confirmação de plano por tarefa, não pare no meio para validar direção.

O documento do sprint **é** o plano aceito: ele já define escopo, ordem e critério de saída. Por isso
o modo automático substitui a etapa 1 abaixo para trabalho de sprint — e só para ele. Trabalho fora
de um sprint continua exigindo plano apresentado e aceito antes do código.

O que **não** muda no modo automático:

- O TDD da etapa 2, inclusive ver o teste falhar antes de implementar.
- Branch, PR e o checklist de merge das etapas 3 a 5.
- Os invariantes acima. Nenhum deles é negociável por pressa de fechar o sprint.

O que ainda interrompe — lista curta e fechada, decidida de antemão:

1. **Ação destrutiva ou irreversível fora do fluxo normal:** `push --force`, reescrita de histórico,
   migração que descarta coluna com dado, exclusão de dado de usuário, rotação de credencial.
2. **Credencial ou acesso que falta** e que só o operador humano pode fornecer.
3. **Tarefa que só faz sentido de duas maneiras materialmente diferentes, ambas caras de desfazer.**
   Se uma das leituras é claramente mais provável, ou se refazer é barato: escolha, registre a
   suposição no PR, e siga.

Fora dessas três, decida e siga. Divergência da spec vira ADR em `doc/adr/`, não pergunta.
Teste que não passa sem uma ferramenta que não existe vira relato no relatório final, não pausa.

Ao terminar o sprint, entregue um relatório único: o que foi feito, quais suposições foram
assumidas, o que ficou fora do escopo e por quê, e o estado do critério de saída.

### 1. Planejar antes de implementar

Fora do modo automático (etapa 0), nenhuma implementação começa sem um plano de implementação
explícito, apresentado e aceito antes de escrever código. O plano diz o que muda, em quais arquivos,
em que ordem, e como será testado.

### 2. TDD — teste primeiro, implementação depois

Para toda feature, nesta ordem:

1. Escrever os testes que descrevem o comportamento desejado.
2. Rodar e **ver falhar** — um teste que passa antes da implementação não está testando nada.
3. Implementar até passar.
4. Refatorar com os testes verdes.
5. Avisar caso algum teste não passou e precisar de uma ferramenta para passar
6. Execute o CI/CD

Como rodar a suíte está em **Ambiente e testes**, no topo deste arquivo: `direnv allow .` + `pytest`
no devshell, ou `docker compose run --rm worker pytest`.

Toda implementação tem teste. Sem exceção.

### 3. Branches

Nunca commitar direto na `main`. Todo trabalho sai em branch, **com nome em inglês**:

| Tipo | Prefixo | Exemplo |
| --- | --- | --- |
| Feature | `feat/` | `feat/telegram-webhook` |
| Correção de bug | `hotfix/` | `hotfix/session-timeout-race` |
| Documentação | `doc/` | `doc/adr-vector-store` |

### 4. Pull request via `gh`

```bash
# criar o PR a partir da branch atual
gh pr create --base main --title "<título>" --body "<o que muda e por quê>"

# solicitar a revisão do Codex imediatamente após criar o PR
gh pr comment --body '@codex review'

# ler os comentários da revisão (obrigatório antes do merge)
gh pr view --comments
gh pr view <n> --json reviews,comments

# verificar se o CI passou (obrigatório antes do merge)
gh pr checks

# mergear
gh pr merge --squash --delete-branch
```

Todo PR novo, sem exceção, deve receber o comentário `@codex review` logo depois de ser criado.
Não presuma que o comentário significa aprovação: aguarde a revisão terminar, leia os apontamentos
e enderece cada um antes do merge.

### 5. Checklist antes de mergear

Verificar os cinco, sempre, nesta ordem:

1. **Revisão do Codex concluída** — o PR recebeu `@codex review` e a revisão terminou. Não mergeie
   enquanto a revisão estiver pendente.
2. **Comentários lidos e endereçados** — confirme com `gh pr view --comments` e
   `gh pr view <n> --json reviews,comments`. Não mergeie por cima de comentário não respondido.
3. **CI verde** — `gh pr checks`. Se retornar "no checks reported", isso não é aprovação: significa
   que não há workflow configurado para a mudança.
4. **Testes passando localmente.**
5. **Revisado contra a spec** — a implementação corresponde à seção correspondente de
   `doc/spec.md`? Divergência é ou bug de implementação ou ADR novo. Nunca divergência silenciosa.

### 6. Sprints

O trabalho segue o roadmap da §24 da spec (Fase 1.0 → 1.1 → 1.2 → 1.3 → 2.0), quebrado em sprints
de 2 semanas rastreados em `doc/sprints/` — comece por `doc/sprints/README.md`. Cada sprint define
escopo, critério de saída verificável e as seções da spec que cobre. Uma fase só é dada por
concluída quando seu critério de saída da §24 é atingido — não quando o código foi escrito.

Sprints são executados em **modo automático** (etapa 0): todas as tarefas do sprint de ponta a
ponta, sem pedir aprovação entre elas.

> A ordem de canais mudou na spec v2.0: **Telegram é a fase 1.0 e WhatsApp virou a fase 2.0**.
> Sprint escrito antes disso pode citar WhatsApp onde hoje se lê Telegram — a §24 é a referência.

## Estado do repositório

```
fitness-track/          ← raiz do projeto e raiz do git
├── .git/
├── .gitignore
├── CLAUDE.md
└── doc/
    └── spec.md
```

Remote: `github.com/FelipeMarcelino/fitness-track`, branch padrão `main`.
