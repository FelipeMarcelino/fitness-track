# ADR-0006 — Áudio longo demais continua sendo `voice`

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-02 |
| Revisa | — (fecha uma lacuna entre a §11.3 e a §18.4, prevista em comentário na S02-T02) |

## Contexto

A §11.3 tem duas exigências sobre um áudio acima do teto de 5 minutos: **não transcrever** e
**pedir para o usuário dividir a gravação**. A §18.4 acrescenta uma terceira, geral: nunca
silenciar.

O parser do Telegram entregue na S02-T02 atendia a primeira e impedia a segunda. Uma gravação acima
do teto voltava como `kind="other"` sem `media_ref` — nada a jusante conseguia buscá-la, o que era o
objetivo, mas `other` é também o rótulo de uma reação a descartar e de uma mudança de participação a
tratar. Com os três casos sob o mesmo rótulo, nada além do payload bruto distinguia "gravação longa
demais, responda pedindo para dividir" de "evento que não é mensagem, descarte". O
`_is_processable` do ingress descartava `other`, então o áudio longo saía do pipeline em silêncio.

O comentário no próprio `_classify_message` registrava a lacuna e antecipava a solução: *"a ingress
tem de distinguir os três a partir do payload... dar um campo à §18.1 em vez disso é um ADR"*.

## Decisão

Uma gravação acima do teto continua sendo `kind="voice"` e **perde** o `media_ref`. O
`InboundMessage` da §18.1 ganha `media_duration_s`, preenchido pelo parser para voz, áudio e
`video_note`, e o envelope do buffer passa a carregar `duration_s`.

O `VoiceIngestion` (S02-T07) é quem aplica a regra: duração acima do teto — ou um item de voz sem
`media_ref`, que só pode ter vindo daí — recebe a resposta fixa de `config/prompts/stt_too_long.md`,
sem download e sem chamada de STT, e não entra no lote.

## Consequências

- A regra da §11.3 passa a ser observável e testável no serviço que a possui, com o número em mão,
  em vez de inferida da ausência de uma referência de mídia.
- A garantia da S02-T02 se mantém: sem `media_ref`, nada a jusante consegue buscar o arquivo.
- `other` volta a significar uma coisa só: evento que não é mensagem do usuário.
- Um item de voz agora chega ao buffer sem `media_ref`, o que antes não acontecia. O serviço de STT
  trata esse caso explicitamente; qualquer outro consumidor de item de voz precisa fazer o mesmo.
- O teste `test_audio_past_the_limit_is_not_transcribed` da S02-T02 mudou de expectativa junto com
  o comportamento — de `kind == "other"` para `kind == "voice"` com `media_ref is None`.

## Condição de revisão

Reabrir se a §18.1 ganhar um tipo dedicado para "mídia recusada antes do download", que hoje não
existe. Nesse caso `media_duration_s` deixa de ser o portador da decisão e volta a ser só
metadado.
