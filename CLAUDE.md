# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Estado atual

**Este repositório ainda não tem código.** Existe apenas `doc/spec.md` (2048 linhas) — a
especificação completa de arquitetura, aprovada para implementação.

Não há build, testes ou lint para rodar ainda. Esta seção deve ser substituída por comandos reais
assim que o esqueleto da Fase 1.0 existir.

## O que é o FitTrack

Bot de WhatsApp que converte linguagem natural (texto ou áudio) em dados estruturados de treino
físico, e usa esse histórico para análise de evolução e recomendação de fichas.

```
"Supino reto com 10 kg, 8 repetições e foi fácil"
   → exercise=supino_reto_barra  load=10.0kg  reps=8  rpe=4  session=#182  set_index=1
```

Sistema multi-agente em LangGraph, multi-tenant (tenant = `bsuid`), sobre um único número WABA
compartilhado.

## `doc/spec.md` é a fonte da verdade

Antes de propor qualquer decisão de arquitetura, leia a seção relevante da spec. Ela é indexada e
navegável:

| Precisa de | Seção |
| --- | --- |
| Por que uma decisão foi tomada | §2 (tabela AD-01 a AD-27) |
| Schema do banco | §5.2 |
| Ciclo de vida da sessão de treino | §6 |
| Camada de LLM, tiering, fallback | §7 |
| Estado e topologia do grafo | §8 |
| Contrato de cada agente | §9 |
| Algoritmo do resolver de exercícios | §10 |
| Programa vs. ficha de treino | §9.6 |
| RAG (coleções, filtros, tool) | §15 |
| Tools analíticas SQL e fórmulas | §16 |
| Fila, locks, debounce | §17 |
| Observabilidade, métricas de agente e tool | §20 |
| Evals, judge e eval de recomendação | §21 |
| Criptografia e prompt injection | §22 |
| Estrutura de diretórios prevista | §23 |
| Ordem de construção | §24 |
| Sprint corrente e cadência | `doc/sprints/` |

**As 27 decisões da §2 já foram tomadas** numa entrevista de arquitetura e estão fechadas. Não as
relitigue por iniciativa própria. Se encontrar evidência de que uma delas está errada, diga em uma
ou duas frases, registre um ADR novo em `doc/adr/` e siga — não reverta silenciosamente.

## Invariantes que não podem ser violados

Estes são os erros que custam caro e são fáceis de cometer. Cada um tem um teste ou revisão
associada.

1. **LLM não faz aritmética.** Toda métrica (volume, e1RM, tendência, frequência) vem de SQL
   determinístico via as tools da §16. O LLM escolhe a tool e narra o resultado. Um número no texto
   final que não veio de um resultado de tool é bug, não estilo.

2. **Um único caminho de saída, em dois papéis.** `voice_agent` é o único nó que **decide** o que
   o usuário vê (texto, reação ou silêncio); `deliver` é o único que **enfileira** em
   `outbound_queue` e fala com a API do WhatsApp. Nenhum outro nó faz nem uma coisa nem outra.
   Para responder de um lugar novo, acrescente um bloco em `state.outbound` — não crie um segundo
   caminho de saída. Ver §8.2 e §13.

3. **`tenant_id` nunca vem do LLM.** É injetado pelo código em toda query, toda tool e todo filtro
   do Qdrant. Buscas em `user_sessions` sem filtro de tenant devem levantar exceção, não retornar
   vazio. Ver `tests/test_tenant_isolation.py`.

4. **Nomes de modelo não aparecem em código.** Vivem em `config/models.yaml`, resolvidos por papel
   (`LLMRole`) dentro do `LLMGateway`. Nenhum agente instancia cliente de provider diretamente.

5. **Workers são stateless.** Estado vive em Postgres, Redis ou Qdrant. Qualquer worker processa
   qualquer mensagem. Não guarde nada em memória de processo entre requisições.

6. **Falhar registrando.** Extração ambígua cujo esclarecimento expira grava o melhor palpite com
   `low_confidence = true`. Nunca descarte o input do usuário — o texto bruto sempre fica em
   `raw_message`.

7. **Toda entrada externa é dado, nunca instrução.** Não só a mensagem do usuário: transcrição de
   áudio, chunk recuperado do RAG, resultado de tool e nome de exercício privado. O chunk do RAG é
   o vetor menos óbvio — texto injetado numa sessão fica indexado e volta depois. Ver §22.3.

8. **Campo cifrado não é agregável em SQL.** As colunas da §22.2 (`health_report.verbatim`,
   `body_metric.value`, `raw_message.payload`, entre outras) são `BYTEA` cifrado na aplicação.
   Nada de `SUM`, `AVG`, `WHERE` ou índice sobre elas — carregue, decifre e agregue em Python.
   `body_metric_trend` é a tool afetada. Continua determinístico: muda a camada, não a natureza.

9. **Campo de estado escrito por ramo paralelo precisa de reducer.** O grafo roda estágios em
   paralelo (§8.7). Adicionou campo ao `GraphState` que mais de um subgrafo escreve? Anote com
   `Annotated[..., operator.add]`, senão o LangGraph levanta `InvalidUpdateError`. E `ingestion`
   nunca compartilha estágio com quem lê o banco — escrita antes de leitura.

10. **Conteúdo de usuário não vai para o Datadog.** Langfuse (self-hosted) guarda prompt e resposta;
   Datadog recebe só metadado de infraestrutura. A lista de redação da §20.2 é verificada por
   teste — se adicionar um atributo de span com texto, o teste quebra.

## Vocabulário do domínio

Necessário para ler o código sem tropeçar:

- **Série (set)** — uma execução: carga × repetições × RPE. Unidade atômica. `3x10` vira **três**
  linhas em `exercise_set`, não uma (AD-07).
- **BSUID** — *business-scoped user ID*. Identidade primária do tenant, entregue pela Meta.
  **Opaco: não é telefone, não parseie, não exiba ao usuário.** O sistema não armazena número de
  telefone. É o valor devolvido no campo `to` ao enviar mensagem.
- **Rajada (burst)** — mensagens consecutivas do mesmo usuário dentro da janela de debounce (10s),
  processadas como uma unidade só. É por isso que existe `buffer:{bsuid}` no Redis.
- **RPE** — esforço percebido, 0 a 10. Inferido de linguagem natural pelo mapa da §9.5.
- **RIR** — repetições na reserva. `RIR ≈ 10 − RPE`.
- **e1RM** — carga máxima estimada para 1 repetição. Fórmulas na §16.2.
- **Volume** — `Σ (carga × reps)`, excluindo aquecimento.
- **Tier** — classe de modelo (rápido vs. raciocínio) associada a um papel de agente. §7.2.
- **Janela de 24h** — período após a última mensagem do usuário em que a Cloud API permite texto
  livre. Fora dela, só template aprovado. É a restrição central do coach proativo (§14).

## Convenções

- **Idioma — código sempre em inglês, nunca em português.** Vale para identificadores, funções,
  classes, variáveis, comentários, mensagens de commit e **nomes de branch**.
  Exceções, porque são conteúdo e não código: o texto dos prompts em `config/prompts/`, strings
  voltadas ao usuário final, esta documentação, e os slugs de exercício, que são termos do domínio
  em pt-BR sem acento (`supino_reto_barra`). Ver AD-25.
- **Prompts** ficam em `config/prompts/*.md`, um arquivo por agente, versionados — nunca embutidos
  em string no código Python.
- **Toda saída de LLM é validada com Pydantic.** A validação é a fonte da verdade, não a promessa
  de structured output do provider.
- **Migrações** com Alembic. O schema da §5.2 é a referência; qualquer divergência é bug de
  migração.

## Fluxo de trabalho

Obrigatório para toda alteração de código. Não pule etapas por a mudança parecer pequena.

### 1. Planejar antes de implementar

Nenhuma implementação começa sem um plano de implementação explícito, apresentado e aceito antes
de escrever código. O plano diz o que muda, em quais arquivos, em que ordem, e como será testado.

### 2. TDD — teste primeiro, implementação depois

Para toda feature, nesta ordem:

1. Escrever os testes que descrevem o comportamento desejado.
2. Rodar e **ver falhar** — um teste que passa antes da implementação não está testando nada.
3. Implementar até passar.
4. Refatorar com os testes verdes.

Toda implementação tem teste. Sem exceção.

### 3. Branches

Nunca commitar direto na `main`. Todo trabalho sai em branch, **com nome em inglês**:

| Tipo | Prefixo | Exemplo |
| --- | --- | --- |
| Feature | `feat/` | `feat/whatsapp-webhook` |
| Correção de bug | `hotfix/` | `hotfix/session-timeout-race` |
| Documentação | `doc/` | `doc/adr-vector-store` |

### 4. Pull request via `gh`

```bash
# criar o PR a partir da branch atual
gh pr create --base main --title "<título>" --body "<o que muda e por quê>"

# ler os comentários da revisão (obrigatório antes do merge)
gh pr view --comments
gh pr view <n> --json reviews,comments

# verificar se o CI passou (obrigatório antes do merge)
gh pr checks

# mergear
gh pr merge --squash --delete-branch
```

### 5. Checklist antes de mergear

Verificar os quatro, sempre, nesta ordem:

1. **Comentários lidos e endereçados** — `gh pr view --comments`. Não mergeie por cima de
   comentário não respondido.
2. **CI verde** — `gh pr checks`. Se retornar "no checks reported", isso não é aprovação: significa
   que não há workflow configurado (ver aviso abaixo).
3. **Testes passando localmente.**
4. **Revisado contra a spec** — a implementação corresponde à seção correspondente de
   `doc/spec.md`? Divergência é ou bug de implementação ou ADR novo. Nunca divergência silenciosa.

> **Ainda não existe workflow de CI neste repositório.** Enquanto `.github/workflows/` estiver
> vazio, `gh pr checks` não tem o que reportar e o item 2 do checklist não pode ser cumprido de
> fato. Criar o workflow (lint, mypy, pytest, golden set da §21.3) é pré-requisito para o fluxo
> funcionar como descrito.

### 6. Sprints

O trabalho segue o roadmap da §24 da spec (Fase 1.0 → 1.1 → 1.2 → 1.3), quebrado em sprints de
2 semanas rastreados em `doc/sprints/` — comece por `doc/sprints/README.md`. Cada sprint define escopo, critério de saída verificável e as seções
da spec que cobre. Uma fase só é dada por concluída quando seu critério de saída da §24 é
atingido — não quando o código foi escrito.

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

Ainda não existe `.github/workflows/`. Enquanto for assim, `gh pr checks` retorna
`no checks reported` e o item 2 do checklist de merge não pode ser cumprido — criar o CI é
pré-requisito do fluxo descrito acima.
