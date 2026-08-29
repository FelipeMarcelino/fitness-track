# Sprint 02 — Telegram Inbound Pipeline

| Campo | Valor |
| --- | --- |
| Fase | 1.0 — Registro confiável |
| Duração | 2 semanas |
| Estado | `planned` |
| Objetivo | Receber, verificar e enfileirar mensagens do Telegram como rajadas persistentes, prontas para o grafo LangGraph da sprint seguinte |
| Referências principais | spec §§4, 8.7, 8.8, 11, 17, 18.1, 18.2, 18.4, 20.6, 22.3, 23 |

## Resultado esperado

Ao final da sprint, um `update` do Telegram chega por webhook (ou polling em dev), é verificado,
deduplicado, persistido em `raw_message` e acumulado no buffer por tenant. Passada a janela de
debounce, um worker ARQ drena o buffer atomicamente, adquire o lock por tenant e monta o
`processing_batch` persistido — o ponto exato em que a Sprint 03 conecta o grafo LangGraph.

O adaptador implementa a interface `Channel` completa (§18.1) com saída funcional: `send`,
`classify_error`, reações, botões e mídia inline. O `deliver` do futuro grafo e qualquer degradado
para "não suportado" já têm por onde falar.

Esta sprint não chama LLM, não executa grafo e não define o que o usuário recebe de resposta além
das mensagens fixas de degradação (imagem/documento fora de escopo).

## Escopo

Incluído:

- interface `Channel`, `ChannelCaps`, `InboundMessage`, `OutboundBlock`, `ErrorClass` e o registry
  por `kind`;
- `TelegramAdapter` completo: `verify` (secret token), `parse` (todos os tipos da §18.2),
  `download_media`, `send` (texto, reação, botões, mídia inline, typing) e `classify_error` com a
  taxonomia da §18.4;
- ingress FastAPI com `POST /webhook/telegram`, verificação em tempo constante, dedup por
  `update_id` no Redis e resposta 200 < 200 ms;
- resolução/bootstrap de identidade via a fronteira pré-tenant da Sprint 01, com cache
  `identity:{channel}:{hash}` (§17.1);
- `raw_message` persistido antes de qualquer processamento (invariante 6);
- buffer por tenant + debounce com timer renovável (§17.1) e `flush_check` enfileirado no ARQ;
- worker ARQ com fila `default`, lock FIFO por tenant com auto-extend (§17.3) e drain atômico por
  `RENAME` (§17.3);
- `processing_batch` montado e persistido (§4.1) com `combined_text` cifrado desde a criação;
- polling `getUpdates` para desenvolvimento (1 réplica, §18.2) e `bootstrap.py` chamando
  `setWebhook`/`deleteWebhook` conforme o modo;
- STT via Groq para itens de voz do lote (§4, §11), com download em tmpfs e prompt de vocabulário;
- resposta educada de não suportado para foto/documento (§18.2);
- marcação de identidade como `revoked_at` em `my_chat_member` (§18.2);
- retry por classe de erro no caminho de envio (§18.4) dentro do semáforo global do bot.

Fora do escopo:

- grafo LangGraph, checkpointer, agentes e prompts (Sprint 03);
- LLMGateway, tiering e fallback (Sprint 03);
- loop de resposta real (`voice_agent`/`deliver` do grafo);
- onboarding, consentimentos, quota e billing;
- catálogo de exercícios/Qdrant e resolver;
- vínculo entre canais (§18.5) — requer fluxo de admin do grafo;
- métricas/observabilidade instrumentadas (Langfuse/Datadog);
- WhatsApp (fase 2.0).

## Princípios de execução

1. Toda lógica de protocolo vive em `src/fittrack/channels/`; o resto do código conhece apenas a
   interface (AD-39). O teste de arquitetura existente já bloqueia vazamento.
2. O `Ingress` nunca toca LLM, grafo ou regra de negócio: verifica, dedupa, resolve identidade,
   persiste e bufferiza. Responde 200 mesmo quando o processamento falhar depois.
3. `external_id` é opaco: vai para `raw_message.payload` cifrado, para `external_id_hash` via
   pepper, e para cache Redis como hash. Nunca em log, span ou métrica (§20.6, invariante 10).
4. Todo erro de envio passa por `classify_error`; nenhum retry cego. O semáforo global do bot e o
   respeito a `retry_after` são parte do adaptador.
5. Nenhum comportamento começa sem teste que falhe pelo motivo esperado. Se o primeiro teste exige
   rede, abstrai-se o cliente HTTP e usa-se fixture gravada.
6. `TELEGRAM_MODE` escolhe webhook ou polling; polling declara uma réplica e `bootstrap.py`
   reconcilia `deleteWebhook` antes de entrar (§18.2).

## Dependências

```text
S02-T01 Channel interface ───┐
   ├── S02-T02 Telegram adapter ──┐
   │                               ├── S02-T03 Ingress webhook ───┐
   └── S02-T03 Ingress webhook ───┤                               │
                                  ├── S02-T04 Buffer and debounce ─┼── S02-T06 Delivery and error classes
                                  │                                │
   S02-T05 Batch drain ────────────┘                                │
   S02-T07 STT fallback ────────────┘                               │
                                                                    ▼
                                                          S02-T08 Bootstrap polling
```

T01 e T02 podem ser implementadas em paralelo? Não: T02 depende de T01, mas T03 depende apenas de
T01, então T02 e T03 avançam juntos depois de T01. T04 e T05 dependem de T03. T06 depende de T02.
T07 e T08 dependem de T05. T08 fecha a sprint integrando tudo.

## Tarefas

### S02-T01 — Channel interface and registry

**Objetivo.** Definir o contrato que todo canal implementa e os tipos que atravessam a fronteira.

**Spec.** §18.1, §18.4 (ErrorClass), §23.

**Depende de:** Sprint 01.

**Arquivos previstos:**

- `src/fittrack/channels/base.py`;
- `src/fittrack/channels/registry.py`;
- `tests/unit/test_channel_contract.py`.

**Plano de implementação:**

1. Escrever teste que falha pela ausência de `Channel`, `ChannelCaps`, `InboundMessage`,
   `OutboundBlock` e `ErrorClass`.
2. Definir `ChannelCaps` como dataclass frozen com todos os campos da §18.1.
3. Definir `InboundMessage` e `OutboundBlock` frozen; `reply_to` como tupla
   `(channel, channel_message_id)`.
4. Definir `Channel` como `Protocol` com `kind: ClassVar` e `caps: ClassVar`, e os métodos de
   entrada e saída da §18.1.
5. Definir `ErrorClass` como `StrEnum` com os seis valores da §18.4.
6. Implementar `registry.py`: mapa `kind: Literal["telegram","whatsapp"]` → classe do adaptador,
   construído a partir de `settings.channels` exatamente como a spec manda (§18.1/Apêndice A).
7. Testar que adaptador rejeita `reply_to[0] != self.kind` e que o registry se recusa a construir
   canal sem credencial.

**Critérios de aceite:**

- contrato importável e tipado; mypy estrito passa;
- registry só monta adaptadores listados em `FITTRACK_CHANNELS`;
- `reply_to` é uma tupla e o desvio de canal é rejeitado antes de qualquer HTTP;
- nenhum código fora de `channels/` importa os tipos concretos do Telegram (o registry devolve
  `Channel`).

### S02-T02 — Telegram adapter

**Objetivo.** Implementar a interface para Telegram, sem rede real obrigatória nos testes.

**Spec.** §18.2, §18.4, §20.6 (redação de `file_path` e token).

**Depende de:** S02-T01.

**Arquivos previstos:**

- `src/fittrack/channels/telegram/adapter.py`;
- `src/fittrack/channels/telegram/client.py`;
- `src/fittrack/channels/telegram/secret.py`;
- `src/fittrack/channels/telegram/markup.py`;
- `tests/unit/test_telegram_adapter.py`;
- `tests/unit/test_telegram_errors.py`.

**Plano de implementação:**

1. Escrever testes com `httpx.MockTransport` ou um `Transport` fake injetado no cliente.
2. `verify`: comparar `X-Telegram-Bot-Api-Secret-Token` com o valor configurado usando
   `hmac.compare_digest`; falha levanta exceção de autenticação antes do parsing.
3. `parse`: mapear `message.text`, `message.voice`, `message.audio`, `message.video_note`,
   `callback_query`, `message.photo`, `message.document`, `message_reaction`, `my_chat_member`.
   `callback_query` vira `kind="button_reply"` com `button_payload`. Vozes mapeiam
   `media_ref=file_id`. `video_note` é tratado como voz quando a duração couber no limite da §11.
4. `download_media`: `getFile` → baixa em tmpfs (`/tmp`), respeitando 20 MB e timeout; nunca loga
   `file_path` (§20.6).
5. `send`: texto com `parse_mode=HTML` e `link_preview_options.is_disabled`; reação com
   `setMessageReaction`; botões com `inline_keyboard` e `callback_data="opt:<idx>"` de até 64
   bytes; mídia com `sendPhoto` multipart inline; typing com `sendChatAction`.
6. `classify_error`: tabela da §18.4 para Telegram — `429`→`RETRY_AFTER` (lê
   `parameters.retry_after`), `403 blocked/deactivated`, `400 chat not found`→`UNDELIVERABLE`,
   `401`→`ACCOUNT`, `5xx`/timeout→`RETRY_BACKOFF`, demais `400`→`BUG`. `message is not modified`
   é sucesso, não erro. `400 message to react not found` → `BUG` com fallback para texto (§18.4).
7. Testar que nenhum log, exceção ou `repr` de domínio contém token do bot ou `file_path`
   (invariante 10).
8. Conectar o adaptador com `redis.asyncio` de forma injetável para dedup e buffer (o adapter não
   abre conexões por si).

**Critérios de aceite:**

- todos os tipos de update da tabela §18.2 parsing para `InboundMessage` correto, incluindo
  `video_note` como voz;
- `verify` rejeita token errado e aceita o certo em tempo constante;
- `classify_error` cobre toda a tabela do Telegram com testes parametrizados, incluindo o fallback
  de reação para texto;
- mídia com caption ≤ 1024 e botões ≤ 8 ≤ 64 bytes conforme `caps`;
- nenhum fixture ou assert imprime token/`file_path`;
- marcas de redação da §20.6 respeitadas.

### S02-T03 — Ingress webhook

**Objetivo.** Endpoint público que verifica, deduplica, resolve identidade, persiste `raw_message`
e bufferiza em <200 ms.

**Spec.** §4, §18.2 (segurança do webhook), §17.4 (idempotência), §19.1 (fronteira pré-tenant),
§22.3 (payload como dado).

**Depende de:** S02-T01. Pode ocorrer em paralelo com S02-T02.

**Arquivos previstos:**

- `src/fittrack/main.py` (rotas `/webhook/telegram`, `/health`, `/metrics`);
- `src/fittrack/services/identity.py` (resolvedor via a fronteira da Sprint 01);
- `tests/integration/test_telegram_webhook.py`;
- `tests/integration/test_webhook_identity_cache.py`.

**Plano de implementação:**

1. Escrever teste de integração falhando pela ausência do endpoint.
2. `POST /webhook/telegram`: chama `adapter.verify`; 403 sem processar se falhar.
3. Dedup `seen:telegram:{hash}:{update_id}` via `SET NX EX 86400`; se existir, 200 rápido e fim.
   **A reserva é recoverable:** se qualquer passo posterior falhar (identidade, banco, buffer), a
   chave é deletada para que o Telegram possa reentregar. O `SET NX` é o primeiro passo, mas o
   sucesso só é confirmado após `raw_message` persistido e item no buffer.
4. Resolver identidade pelo serviço `identity.py` com cache `identity:telegram:{external_id_hash}`
   (TTL 5 min) antes de decifrar; primeiro contato cria tenant + identidade pela fronteira
   pré-tenant da Sprint 01.
5. Persistir `raw_message` com payload cifrado e `(identity_id, channel_message_id)` — segunda
   barreira de idempotência (§17.4).
6. Filtrar antes do buffer: `message_reaction` é ignorado (não gera processamento); `photo` e
   `document` recebem a resposta fixa de degradação (T06) e não entram no buffer. Somente
   `text`, `voice`, `audio`, `video_note` e `callback_query` entram no buffer.

   > **Nota da S02-T02.** O adaptador entrega `kind="other"` para três coisas diferentes: o
   > `message_reaction` (descartar em silêncio), o `my_chat_member` com status `kicked`/`blocked`
   > (revogar a identidade, §18.2) e o **áudio acima do teto de 5 min da §11.3** (pedir para
   > dividir). Só a primeira pode virar silêncio. O `kind` da §18.1 é um `Literal` fechado e não
   > distingue as três, então o filtro precisa olhar o `raw` para separá-las — ou a §18.1 ganha um
   > campo, o que é um ADR. Áudio longo que não recebe resposta viola a §18.4 ("nunca silêncio").
7. `RPUSH buffer:{tenant_id}` com envelope JSON `{channel, external_id_hash, channel_message_id,
   kind, text, media_ref, button_payload, sent_at, raw_message_id}`.
8. `SET debounce:{tenant_id} 1 EX 10` e enfileira `flush_check(tenant_id)` com delay de 10s no
   ARQ, usando job ID estável `flush:{tenant_id}` para que renovações não acumulem jobs.
9. Responder 200 sempre — mesmo se o worker estiver morto.
10. Testar rajada: 4 mensagens seguidas resultam em 4 itens no buffer e um único timer ativo.
11. Testar que `external_id` (chat.id) nunca aparece em logs ou spans — somente o hash.

**Critérios de aceite:**

- webhook responde < 200 ms com Redis e Postgres reais no ambiente de CI;
- dedup por `update_id` bloqueia reentrega e a reentrega vazia é descartada sem lookup;
- falha após `SET NX` deleta a reserva para permitir reentrega;
- `raw_message` cifrado + entrada no buffer acontecem na mesma requisição;
- primeiro contato cria identidade somente via fronteira autorizada da Sprint 01;
- cache de identidade evita decifrar em rajada e expira em 5 min;
- `external_id` nunca em log/spans (teste de captura de logging);
- `message_reaction`, `photo` e `document` não entram no buffer.

### S02-T04 — Buffer and debounce

**Objetivo.** Janela móvel de 10s por tenant que acumula, depois libera, a rajada.

**Spec.** §4, §17.1, §17.3 (drain atômico).

**Depende de:** S02-T03.

**Arquivos previstos:**

- `src/fittrack/services/debounce.py`;
- `src/fittrack/worker.py` (função `flush_check`);
- `tests/integration/test_debounce_flush.py`.

**Plano de implementação:**

1. Escrever teste que falha porque `flush_check` não existe.
2. `flush_check(tenant_id)`: se `debounce:{tenant_id}` ainda existe, reenfileira com 10s de novo.
   O job ID é estável (`flush:{tenant_id}`), então renovações substituem o job anterior em vez de
   acumular.
3. Se expirou, adquire `lock:{tenant_id}` com TTL 120s e auto-extend; se ocupado, reenfileira com
   5s (§17.3).
4. Com o lock, `RENAME buffer:{tenant_id} → drain:{tenant_id}:{batch_id}` e leitura apagada —
   nunca `LRANGE+DEL` sobre `buffer:`.
5. Se `RENAME` falhou por chave ausente, retorna sem enfileirar nada.
6. A chave `drain:` é varrida pelo job de manutenção caso o worker morra entre `RENAME` e `DEL`
   (§17.3).
7. Testar rajada intercalada com debounce expirando durante o drain: mensagens posteriores não
   entram no lote (REMOVE atômico do `RENAME`).

**Critérios de aceite:**

- debounce renova a cada mensagem; flush só dispara após silêncio de 10s;
- job ID estável evita acúmulo de jobs de flush;
- lock por `tenant_id` com auto-extend, e a ocupação reenfileira com 5s;
- drain é atômico e sobrevive ao restart do worker;
- nunca erro de `LRANGE+DEL` sobre `buffer:`.

### S02-T05 — Batch drain and persistence

**Objetivo.** Transformar o drain em `processing_batch` persistido, pronto para `ainvoke` da
Sprint 03.

**Spec.** §4, §17.2 (fila `default`), §4.1 (garantias), §22.2 (`combined_text` cifrado).

**Depende de:** S02-T04.

**Arquivos previstos:**

- `src/fittrack/services/batch.py`;
- `tests/integration/test_process_batch.py`.

**Plano de implementação:**

1. Escrever teste falhando pela ausência de `process_batch`.
2. Para itens com `kind="voice"`: baixar via adaptador, transcrever com STT e entrar no texto com
   `was_audio=true` (a execução do STT entra em T07; aqui é o ponto de integração).
3. Montar envelope do lote na ordem de chegada, sem concatenar (quem junta é o normalizer, §9.3).
4. Persistir `processing_batch` com `combined_text` cifrado (AES-256-GCM, keyring da Sprint 01) e
   `status='pending'` (o schema da Sprint 01 usa `status`, não `batch_status`).
5. Enfileirar `process_batch(tenant_id, batch_id)` na fila `default` do ARQ com `max_tries=3` e
   backoff exponencial (§4.1).
6. A função `process_batch` desta sprint termina aí: marca `status='done'` e para — é o ponto de
   conexão do grafo; log estruturado registra o handoff. O lock por tenant é adquirido dentro de
   `process_batch` (não apenas em `flush_check`), para que a execução do grafo na Sprint 03
   permaneça serializada por tenant.
7. Testar idempotência de retry: re-enfileirar o mesmo `batch_id` não duplica
   `processing_batch` e não refaz o download de voz (guardado por `raw_message_id`).

**Critérios de aceite:**

- `processing_batch.combined_text` é `BYTEA` cifrado desde a primeira escrita;
- fila `default` com `max_tries=3` e backoff, e o estado do batch sobrevive ao worker;
- itens de voz são marcados `was_audio=true` antes da persistência do batch;
- a função `process_batch` deixa o batch em `done` e o `handoff` é auditável;
- o lock por tenant é adquirido dentro de `process_batch`, não apenas em `flush_check`;
- nenhum conteúdo do usuário em log além de `raw_message.payload` cifrado.

### S02-T06 — Delivery and error classes

**Objetivo.** Caminho de envio funcional para degradar foto/documento e para o `deliver` do
futuro grafo, com retry por classe.

**Spec.** §18.4, §18.2 (envio), §17.4 (envio).

**Depende de:** S02-T02.

**Arquivos previstos:**

- `src/fittrack/services/outbound.py`;
- `tests/unit/test_outbound_retry.py`.

**Plano de implementação:**

1. Escrever teste falhando pela ausência do envio com classes.
2. Rate limiter compartilhado via Redis (não semáforo local): o limite global do bot (~30 msgs/s)
   e o espaçamento por chat (≥1s entre bolhas) são coordenados entre os 4 workers. Um semáforo
   local por processo permitiria 4× o limite global.
3. Fila de retry com a escada 2s, 8s, 32s, 2min, 8min e jitter ±25%, e a exceção de
   `RETRY_AFTER` usando o valor literal do canal. O valor de `retry_after` é carregado no
   resultado de `classify_error` (não apenas a classe), para que o serviço de outbound possa
   persistir `next_retry_at` sem conhecer o canal.
4. `RETRY_BACKOFF`: até 5 tentativas; `RETRY_AFTER`: até 5; `UNDELIVERABLE`, `ACCOUNT`, `BUG`:
   não repetem; `DEFER_WINDOW` nunca gerado pelo Telegram (presente no enum por compat).
5. Persistir tentativa em `outbound_queue` com `group_id`/`seq` para toda mensagem, incluindo
   mensagens fixas de degradação. O `group_id` é gerado para cada resposta, mesmo que tenha um
   único bloco.
6. Proativas nunca repetem automaticamente (§18.4) — esta sprint não gera proativas, mas a
   política já está no serviço.
7. Degradação de foto/documento: `OutboundBlock(kind="text")` fixo, redigido em
   `config/prompts/` (exceção de idioma permitida, AD-27).

**Critérios de aceite:**

- backoff respeita escada + jitter; `RETRY_AFTER` usa o número literal;
- rate limiter é compartilhado via Redis entre workers;
- `retry_after` é carregado no resultado de `classify_error` e usado para `next_retry_at`;
- classes não repetíveis são persistidas `dead` com `error_code`;
- toda mensagem em `outbound_queue` tem `group_id`/`seq`, incluindo degradação fixa;
- foto/documento recebe resposta fixa e o caso é registrado.

### S02-T07 — Voice and STT via Groq

**Objetivo.** Itens de voz entram no buffer como texto com `was_audio=true`.

**Spec.** §4 (fluxo), §11 (teto, prompt de vocabulário, regras), §20.6 (redação).

**Depende de:** S02-T05 (ponto de integração) e adaptador de download.

**Arquivos previstos:**

- `src/fittrack/services/stt.py`;
- `config/prompts/stt_vocabulary.md` (exceção de idioma, AD-27);
- `tests/unit/test_stt.py` com cliente injetado.

**Plano de implementação:**

1. Escrever teste falhando pela ausência de `stt`.
2. Download em `/tmp` (tmpfs), apagado após transcrição bem-sucedida; duração limitada pela §11
   (5 min). Em falha de STT, o arquivo é mantido em `/tmp` por até 6h para retry (§11.3), depois
   apagado.
3. POST Groq `/audio/transcriptions` com `whisper-large-v3`, `language=pt`,
   `response_format=verbose_json` (para `no_speech_prob` e segments), `prompt` lido de
   `config/prompts/stt_vocabulary.md`.
4. `file_path` nunca em log (§20.6); destino do arquivo gravado com `O_NOFOLLOW` quando suportado.
5. Transcrição vazia ou `no_speech_prob > 0.6` → resposta fixa "Não consegui ouvir, pode repetir?"
   (§11.3), sem entrar no batch.
6. Falha de STT (erro de rede, timeout, 5xx) → o item é marcado `was_audio=true` com texto vazio
   e `status='incomplete'` no batch (invariante 6), e o arquivo é mantido para retry.
7. Transcrição bem-sucedida → o texto é persistido em `raw_message.transcript` (cifrado) antes de
   apagar o arquivo, para que retry de batch não repita download nem STT.
8. Consentimento: o STT só é chamado se o tenant tiver consentimento `workout_data` ativo. Sem
   consentimento, a voz é respondida com a mensagem fixa de onboarding (fora do escopo desta
   sprint, mas o gate é implementado aqui).

**Critérios de aceite:**

- ogg/opus baixado em tmpfs e apagado após transcrição bem-sucedida;
- falha de STT mantém o arquivo por até 6h para retry;
- prompt de vocabulário carregado de `config/prompts/`;
- `response_format=verbose_json` usado; `no_speech_prob > 0.6` ou texto vazio → resposta fixa;
- duração acima do teto é recusada com resposta fixa;
- nenhum log com `file_path` ou token;
- falha de STT mantém o item registrado como `incomplete`;
- transcrição persistida em `raw_message.transcript` antes de apagar o arquivo;
- STT só é chamado com consentimento `workout_data` ativo.

### S02-T08 — Bootstrap polling and integration

**Objetivo.** Fechar o caminho do clone limpo até mensagem processada (sem grafo), com polling
para dev e webhook documentado.

**Spec.** §18.2 (polling), §4, §21.4.

**Depende de:** S02-T04, T05, T06, T07.

**Arquivos previstos:**

- `src/fittrack/channels/telegram/polling.py`;
- `scripts/bootstrap.py` (setWebhook/deleteWebhook);
- `tests/integration/test_telegram_pipeline_smoke.py`;
- atualização do `CLAUDE.md` (estado atual) e `README.md`.

**Plano de implementação:**

1. Escrever smoke test integrado falhando: update → buffer → batch `done`.
2. `polling.py`: `getUpdates` long-polling com offset persistido em Redis (para sobreviver a
   restart), somente quando `TELEGRAM_MODE=polling`.
3. `bootstrap.py`: em modo webhook chama `setWebhook` com `secret_token`,
   `allowed_updates=["message","callback_query","message_reaction","my_chat_member"]` e
   `max_connections=40`; em modo polling chama `deleteWebhook` antes de subir o poller (§18.2).
4. `TELEGRAM_MODE` default é `webhook` em produção; `polling` é explicitamente de desenvolvimento
   e o compose de produção não o permite (guarda no settings ou no compose).
5. Documentar: dev roda 1 réplica do `ingress` com polling; produção usa webhook e Caddy.
6. Rodar a suíte duas vezes para demonstrar idempotência do bootstrap.
7. Atualizar `CLAUDE.md` removendo itens entregues da tabela "Estado atual".

**Critérios de aceite:**

- polling só em dev (1 réplica) e `bootstrap.py` reconcilia webhook/polling;
- `allowed_updates` inclui `my_chat_member` para que `revoked_at` seja observável;
- `TELEGRAM_MODE` default é `webhook`; polling exige override explícito;
- smoke test passa de ponta a ponta contra Redis e Postgres reais;
- `CLAUDE.md` reflete o estado real; suíte idempotente;
- nenhum segredo ou payload sensível em log, fixture ou erro.

## Ordem de PRs

| Ordem | Branch sugerida | Tarefa | Pode paralelizar |
| --- | --- | --- | --- |
| 1 | `feat/channel-interface` | S02-T01 | Não |
| 2 | `feat/telegram-adapter` | S02-T02 | Com T03 |
| 3 | `feat/telegram-webhook` | S02-T03 | Com T02 |
| 4 | `feat/debounce-buffer` | S02-T04 | Não |
| 5 | `feat/batch-drain` | S02-T05 | Com T04 (coordenar worker) |
| 6 | `feat/outbound-retry` | S02-T06 | Com T04/T05 |
| 7 | `feat/stt-groq` | S02-T07 | Com T06 |
| 8 | `feat/telegram-bootstrap` | S02-T08 | Não |

## Critério de saída da sprint

A sprint termina somente quando todos os itens abaixo forem demonstrados:

- [ ] `POST /webhook/telegram` verifica o secret em tempo constante e responde < 200 ms;
- [ ] dedup por `update_id` + `(identity_id, channel_message_id)` descarta reentrega sem lookup;
- [ ] falha após `SET NX` deleta a reserva para permitir reentrega;
- [ ] `raw_message.payload` é cifrado e nunca sai em log;
- [ ] rajada acumula em `buffer:{tenant_id}` com debounce de 10s renovável e job ID estável;
- [ ] drain por `RENAME` é atômico com lock por tenant e auto-extend;
- [ ] `processing_batch.combined_text` nasce cifrado e o batch fica em `done`;
- [ ] o lock por tenant é adquirido dentro de `process_batch`, não apenas em `flush_check`;
- [ ] voice é baixada em tmpfs, transcrita via Groq com vocabulário, e apagada;
- [ ] falha de STT mantém o arquivo por até 6h para retry;
- [ ] transcrição persistida em `raw_message.transcript` antes de apagar o arquivo;
- [ ] STT só é chamado com consentimento `workout_data` ativo;
- [ ] todo update type da §18.2 parseia ou é descartado com a política correta, incluindo
  `video_note` e `my_chat_member`;
- [ ] `classify_error` cobre a tabela do Telegram e o retry respeita `retry_after` literal;
- [ ] `retry_after` é carregado no resultado de `classify_error` e usado para `next_retry_at`;
- [ ] rate limiter é compartilhado via Redis entre workers;
- [ ] toda mensagem em `outbound_queue` tem `group_id`/`seq`, incluindo degradação fixa;
- [ ] foto/documento recebe resposta fixa de não suportado;
- [ ] polling funciona em dev com 1 réplica; `bootstrap.py` reconcilia modos;
- [ ] `TELEGRAM_MODE` default é `webhook`; polling exige override explícito;
- [ ] teste de arquitetura `test_channel_isolation` continua verde;
- [ ] CI obrigatório está verde, `make fmt/lint/typecheck/test` passam.

## Riscos e mitigação

| Risco | Impacto | Mitigação nesta sprint |
| --- | --- | --- |
| Lógica de protocolo vazar para fora de `channels/` | Alto — mata o AD-39 | `test_channel_isolation` verde desde a Sprint 01; revisar imports |
| Retry cego duplicar envio | Alto — mensagens duplicadas | `classify_error` obrigatório; `outbound_queue.group_id/seq` |
| `RENAME` de buffer não ser atômico | Crítico — mensagem perdida | somente §17.3, com teste que reprova a alternativa |
| STT depender de rede nos testes | Médio — suíte instável | cliente HTTP injetado; fixtures gravadas; teste de unidade sem rede |
| Polling e webhook ligados ao mesmo tempo | Médio — 409 do Telegram | `bootstrap.py` reconcilia; `TELEGRAM_MODE` exclusivo; dev com 1 réplica |
| `file_path`/token vazarem em log | Crítico — segredo na URL | lista de redação da §20.6 coberta por teste; nunca logar |
| Rate limiter local permitir 4× o limite global | Alto — 429s evitáveis | rate limiter compartilhado via Redis |
| Lock por tenant liberado antes do grafo | Alto — processamento concorrente | lock adquirido dentro de `process_batch` |
| STT sem consentimento | Crítico — LGPD | gate de consentimento `workout_data` antes de chamar Groq |

## Suposições registradas

- A sprint não define resposta ao usuário além da degradação fixa; o handoff para `ainvoke` é o
  contrato com a Sprint 03.
- `identity:{channel}:{hash}` cache usa `external_id_hash` (HMAC com pepper), nunca o valor claro.
- A tabela de erros do WhatsApp entra na fase 2.0 sem mudar o enum — é por isso que `DEFER_WINDOW`
  já existe.
- O polling persiste o offset em Redis e é explicitamente de desenvolvimento; nunca sobe em
  produção.
- O teste de `test_graph_reducers` e `test_graph_topology` continuam fora até a Sprint 03, quando
  o grafo existir.
- O `GROQ_API_KEY` é provisionado apenas ao worker (não ao ingress), e o compose de produção
  reflete isso.

## Relatório de encerramento

Ao concluir a sprint, registrar neste documento:

- PRs mergeados por tarefa;
- handoff com a Sprint 03 (assinatura do `process_batch` e formato do envelope do batch);
- suposições efetivamente usadas;
- itens adiados e motivo;
- estado de cada item do critério de saída;
- riscos novos para a Sprint 03 ou ADR.
