# Sprint 02 — A primeira extração, já medida

| Campo | Valor |
| --- | --- |
| Fase | 1.0 — Registro confiável |
| Duração | 2 semanas |
| Estado | planejado |
| Seções da spec | §7, §9.1, §9.4, §9.5, §10, §15, §20.1–20.2, §21.1 |

## Objetivo

**"supino reto 80 kg 8 reps" vira uma linha em `exercise_set`** — e a acurácia dessa conversão é
um número no CI desde o primeiro commit que a produz.

## Por que este sprint agora

O sprint 01 provou o trilho: mensagem entra, atravessa, volta. Não provou nada sobre qualidade,
porque não havia saída de LLM para julgar. Este sprint produz a primeira, e a ordem interna dele
existe para que ela **nasça medida** em vez de ser medida depois.

Medir depois tem um custo específico e conhecido: quando o golden set chega, já existe regressão
acumulada e ninguém sabe qual mudança causou. É a mesma razão pela qual o harness de avaliação
veio antes do grafo, no sprint 01 — aqui a dívida seria maior, porque extração é justamente o que
degrada em silêncio.

O segundo motivo é de custo, e é o oposto do sprint anterior: **este é o primeiro sprint que gasta
token.** Toda decisão de tiering, fallback e quota da §7 deixa de ser desenho e passa a ter fatura.
Por isso o `LLMGateway` é a tarefa 1 e o `usage_ledger` já começa a ser escrito nela — não como
observabilidade opcional, mas como a única forma de saber quanto custou o que acabamos de rodar.

## Escopo

### Dentro

- `LLMGateway` completo (§7.1): resolução `role → (provider, model, params)`, timeout por papel,
  retry com backoff, fallback entre providers, validação Pydantic, escrita em `usage_ledger`,
  checagem de quota antes da chamada
- `config/models.yaml` com os nove papéis da §7.2, recarregável sem redeploy
- Absorção das diferenças entre providers da §7.4 — em especial **nunca passar `temperature` no
  caminho Anthropic**, que é 400 nos modelos atuais
- Catálogo global semeado: ~300 exercícios em `exercise` (`tenant_id IS NULL`) com aliases em
  `exercise_alias`, mais a coleção `exercise_catalog` no Qdrant (§15)
- `exercise_resolver` (§10) inteiro: alias exato → trigram → vetorial → desempate por LLM →
  fallback de criação privada, **com a precedência do alias do usuário sobre o global**
- Aprendizado de alias: resolução bem-sucedida pelas camadas 2, 3 ou LLM grava `source='learned'`
- `extraction_agent` (§9.4): `ExtractionResult` validado, expansão de `3x10` em três séries,
  conversão de unidades, mapa de RPE da §9.5, regra de nunca inventar
- `session_manager` (§6.1): abre, reutiliza ou reabre sessão. Python puro, sem LLM
- `persistence_agent`: transação única, idempotente por `source_message_id` via `ux_set_idempotency`
- `guardrail_agent` (§12) e `supervisor_agent` (§8.7) na topologia da §8.2, substituindo o
  `echo` do sprint 01
- `voice_agent` (§13) como única saída, com o split por unidade de ideia da §13.6
- Golden set v1: **150 casos** distribuídos pelos buckets da §21.1, rodando contra os **dois**
  providers no CI
- Langfuse no `LLMGateway` (§20.1) e a lista de redação da §20.2 verificada por teste

### Fora — deliberadamente

Cada item aqui tem um motivo, não é esquecimento:

| Fora | Por quê |
| --- | --- |
| `clarification_agent` (§9.7) | Precisa de `interrupt()` e de uma política de campos obrigatórios por tipo de série. É um sprint de conversa, não de extração. Até lá, campo faltando vira `missing_fields` e `low_confidence = true` (invariante 6 do CLAUDE.md) |
| `correction_agent` | "Na verdade era 12 reps" depende de saber o que foi gravado; sem histórico registrado não há o que corrigir. Sprint 03 |
| STT via Groq (§11) | O `voice_stub` do sprint 01 já existe e não faz nada. Áudio multiplica o golden set por um bucket inteiro de transcrição ruidosa |
| `onboarding_agent` + consentimentos LGPD | Máquina de estados própria, com implicação jurídica. Merece o seu sprint |
| `summary_agent` e fechamento de sessão | O `session_manager` abre e reutiliza; fechar por scheduler é §6.2 e puxa o `gamification_agent` junto |
| Datadog / OTel | Langfuse entra porque vive dentro do gateway. O exportador de infraestrutura é trabalho de deploy, e a lista de redação já é testada sem ele |
| Dedup semanal de exercícios privados | Job de manutenção sobre dados que ainda não existem em volume |
| Promoção de alias para global | Precisa de 3 usuários distintos convergindo. Não há 3 usuários |

## Tarefas

Uma PR por tarefa. A ordem importa: cada uma depende da anterior estar mergeada.

### 1. `feat/llm-gateway`

A interface única da §7.1. Nenhum agente instancia cliente de provider — este é o invariante 4 do
CLAUDE.md, e é o momento em que ele passa a valer.

Inclui `config/models.yaml`, o enum `LLMRole`, a política de fallback da §7.3, o mapa de
parâmetros permitidos por provider da §7.4, escrita em `usage_ledger` e `QuotaExceeded`.

**Testes primeiro:** papel resolve para provider e modelo do YAML; `429` no primário faz backoff e
segunda tentativa no mesmo primário antes de cair; ambos falhando levanta para a fila; `400` **não**
cai para o fallback (é erro de programação, e tentar de novo esconde o bug); resposta que não valida
contra o schema faz um retry com mensagem de correção e depois cai; `temperature` é removido no
caminho Anthropic; `was_fallback=true` aparece no resultado e no `usage_ledger`; quota estourada
levanta **antes** de gastar a chamada; `git grep` por nome de modelo em `src/**/*.py` volta vazio.

### 2. `feat/exercise-catalog`

Os ~300 exercícios globais com seus aliases, mais a coleção `exercise_catalog` no Qdrant.

É trabalho de dados, não de código, e por isso é a tarefa com maior risco de virar gargalo — ver
Riscos. O seed é versionado como arquivo, não como migração: catálogo é conteúdo, e uma correção de
nome não deveria exigir uma migração nova.

**Testes primeiro:** seed é idempotente (rodar duas vezes não duplica); todo exercício global tem ao
menos um alias; `normalized` é gerado com `unaccent` e bate com a normalização do resolver — se as
duas divergirem, a camada 1 nunca acha nada e ninguém percebe, porque a camada 2 cobre; contagem no
Qdrant bate com a contagem no Postgres; slug segue a convenção pt-BR sem acento do AD-25.

### 3. `feat/exercise-resolver`

As três camadas da §10 mais o desempate. Determinístico até onde der; LLM só quando as camadas
baratas não decidem.

**Testes primeiro:** alias exato devolve confiança 1.00; **alias do usuário vence o global para o
mesmo texto** (pedido explícito, §10); trigram acima de 0.85 sem empate próximo resolve sem tocar
no Qdrant; empate próximo cai para a camada 3; vetorial exige score ≥ 0.88 **e** gap ≥ 0.06 para o
segundo — só o score não basta, e é esse o caso que a camada existe para separar; desempate por LLM
abaixo de 0.75 não resolve; fallback cria exercício privado com `status='pending_review'` e grava
alias `user`; resolução por camada 2 ou 3 grava alias `learned`; **nenhuma query cruza tenant**.

### 4. `feat/extraction-agent`

O contrato da §9.4. Prompt em `config/prompts/extraction.md`, versionado — nunca embutido em string.

Entra com a primeira fatia do golden set: os três buckets de maior peso da §21.1 (registro simples,
rajada fragmentada, notação `NxM`), cerca de 60 casos. O resto vem na tarefa 7. A fatia entra aqui
porque um extrator sem casos é um extrator não medido, e é exatamente o que este sprint existe para
evitar.

**Testes primeiro:** `3x10` produz `repeat=3, reps=10` e o expansor gera **três** linhas (AD-07);
`12, 10, 8` produz três séries de `repeat=1` com reps distintas; "libras" converte por 0.45359237;
"barra fixa 10 reps" tem `load_kg=null`, e "com 10 kg de lastro" tem `load_kg=10` e
`technique="lastro"`; o mapa de RPE da §9.5 traduz "foi fácil" em 4–5; campo não mencionado vira
`null` e entra em `missing_fields`, nunca um chute; `source_text` presente em toda série; saída
inválida do provider é rejeitada pelo Pydantic mesmo que o provider prometa structured output.

### 5. `feat/session-persistence`

`session_manager` e `persistence_agent`. Os dois são Python puro — nenhum LLM decide se uma sessão
está aberta.

**Testes primeiro:** primeira série sem sessão aberta cria uma; série seguinte dentro da janela
reutiliza e empurra `last_activity_at`; série após 90 min de silêncio abre outra; gravação é uma
transação só — falha no meio não deixa metade das séries; reprocessar o mesmo `source_message_id`
não duplica (`ux_set_idempotency` com `NULLS NOT DISTINCT`); `set_index` é sequencial dentro do
exercício na sessão, porque é o que toda query de progressão lê.

### 6. `feat/ingestion-subgraph`

Substitui o `echo` do sprint 01 pela topologia real da §8.2: `load_context → guardrail →
supervisor → ingestion → voice_agent → deliver`. O `voice_agent` toma o lugar do `voice_stub` e
passa a ser a única saída (invariante 2).

**Testes primeiro:** `guardrail` com veredito `BLOCK` desvia direto para o `voice_agent`, sem passar
pelo supervisor; `supervisor` devolve um plano em estágios válido; o subgrafo `ingestion` escreve em
`extracted_sets` e `persisted_set_ids`; nenhum nó além do `voice_agent` escreve `outbound`;
`deliver` continua o único a enfileirar; o split da §13.6 quebra por unidade de ideia e as bolhas
saem na ordem, apoiadas no `group_id`/`seq` que o sprint 01 já entregou.

### 7. `feat/golden-set-v1`

Completa o dataset a 150 casos, cobrindo todos os buckets da §21.1, e liga a execução contra os
**dois** providers no CI.

Rodar só contra o primário deixaria o fallback ser uma degradação silenciosa: o dia em que o xAI
cair, a qualidade cai junto e o único sinal é o usuário reclamando.

**Testes primeiro:** cada bucket da §21.1 tem o número mínimo de casos declarado e o runner falha se
faltar; os limiares por campo da §21.1 são aplicados individualmente; o job roda contra primário e
fallback e reporta os dois; um caso de prompt injection no dataset não muda o comportamento do
extrator (§22.3).

### 8. `feat/langfuse-tracing`

Langfuse dentro do `LLMGateway` (§20.1) — prompt, resposta, latência, custo, por papel e por tenant.

**Testes primeiro:** toda chamada ao gateway produz um trace com `role`, `provider`, `was_fallback`
e tokens; a lista de redação da §20.2 é verificada — um atributo de span com texto do usuário
**quebra o teste**, que é a única forma de essa fronteira não vazar por descuido; `bsuid` nunca
aparece em atributo de span, só o `tenant_id` interno.

## Critérios de saída

Verificáveis, na ordem em que devem ser conferidos:

| # | Critério | Como verificar |
| --- | --- | --- |
| 1 | Nome de modelo fora do código | `git grep -nE 'grok-\|claude-' -- 'src/**/*.py'` volta vazio |
| 2 | Fallback funciona | Teste força `429` no primário e a resposta vem do fallback com `was_fallback=true` |
| 3 | Catálogo semeado | `SELECT count(*) FROM exercise WHERE tenant_id IS NULL` ≥ 300, e a coleção Qdrant tem o mesmo número de pontos |
| 4 | Resolver acerta | Bucket de ambiguidade do golden set com acurácia ≥ 0.92 (§21.1) |
| 5 | Extração dentro do piso | `is_workout_log` ≥ 0.98, `exercise_slug` ≥ 0.92, `load_kg` ≥ 0.97, `reps` ≥ 0.97, nº de séries ≥ 0.95, RPE com erro médio ≤ 1.0 |
| 6 | Série é atômica | "supino 3x10 80kg" gera **três** linhas em `exercise_set` (AD-07) |
| 7 | Idempotência | Reprocessar o mesmo batch não duplica séries |
| 8 | Dois providers | Golden set roda contra primário e fallback no CI, ambos passam |
| 9 | Judge limpo | Calibração dentro do limite e as duas rubricas bloqueantes (segurança, fidelidade numérica) sem nenhum caso < 5 |
| 10 | Fronteira de dados | Langfuse tem prompt e resposta; o teste da lista de redação da §20.2 passa |
| 11 | Custo visível | `SELECT sum(cost_usd) FROM usage_ledger` devolve o custo da rodada de eval, não zero |
| 12 | Ida e volta real | "supino reto 80 kg 8 reps" pelo celular devolve confirmação e a linha existe no banco |

## Riscos

| Risco | Impacto | Plano |
| --- | --- | --- |
| **150 casos de golden set é trabalho de dados** e vira gargalo do sprint | Alto — atrasa os critérios 4, 5 e 8 | Escrever por bucket, em lotes, a partir da tarefa 4. O mínimo por bucket é declarado e testado; se um bucket ficar curto, o sprint fecha com ele explícito em vez de com o total maquiado |
| Nome ou disponibilidade do modelo xAI diferente do previsto na §7.2 | Baixo | É config, não código: `config/models.yaml` muda numa linha. É precisamente por isso que o invariante 4 existe |
| Custo do judge por PR sai do previsto | Médio | O filtro de caminho da §21.4 já está no CI desde o sprint 01. Se ainda pesar, reduzir a amostra de 40 e registrar no ADR — nunca desligar o judge |
| Structured output divergir entre os dois providers | Médio | A validação Pydantic é a fonte da verdade (§7.4, item 3), não a promessa do provider. O teste que rejeita saída inválida cobre os dois caminhos |
| Normalização do resolver divergir da do seed | **Alto e silencioso** | A camada 1 pararia de achar e a camada 2 cobriria o buraco, com custo e latência maiores e nenhum erro. Teste dedicado na tarefa 2 |
| Embeddings exigirem `OPENAI_API_KEY` real | Médio | Já está no `.env.example`. Sem ela, a camada 3 não sobe e o resolver cai para o desempate por LLM — degradação, não quebra |
| Quota estourar durante a rodada de eval | Baixo | `QuotaExceeded` levanta antes da chamada. O eval roda com um tenant de teste sem limite |

## Dívida que este sprint quita

Quatro variáveis — `XAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY` — são hoje
obrigatórias para o app subir e **nenhuma é usada**. O sprint 01 as exigia por antecipação, o que
obriga quem sobe o projeto a inventar valores para credenciais que não fazem nada.

Este sprint passa a usar as três primeiras de verdade. A quarta (`GROQ_API_KEY`, STT) continua sem
uso até o áudio entrar, e deve virar opcional na tarefa 1 — exigir credencial que não é lida é o
mesmo tipo de falha adiada que o validador de campo em branco existe para prevenir.

## O que este sprint deliberadamente não prova

Que o bot conversa. Ele registra, e responde o que registrou — mas não pergunta o que faltou, não
aceita correção, não ouve áudio e não fecha a sessão com um resumo. Tudo isso é sprint 03, e
depende de existir histórico registrado para ter sobre o que conversar.
