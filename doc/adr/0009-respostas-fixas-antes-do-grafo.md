# ADR-0009 — Respostas fixas da §11.3 antes de o `voice_agent` existir

| Campo | Valor |
| --- | --- |
| Estado | aceito, com prazo |
| Data | 2026-09-03 |
| Revisa | — (registra uma tensão temporária com a invariante 2 do `CLAUDE.md`) |
| Expira | Sprint 03, quando `voice_agent` e `deliver` entrarem |

## Contexto

A invariante 2 diz: **`voice_agent` é o único nó que decide o que o usuário vê; `deliver` é o único
que enfileira em `outbound_queue`.** Nenhum dos dois existe: eles nascem com o grafo, na Sprint 03.

A §11.3, por outro lado, exige três respostas **agora**, e com texto fixo:

- `"Não consegui ouvir, pode repetir?"` para transcrição vazia ou `no_speech_prob > 0.6`;
- pedido para dividir a gravação acima do teto de 5 minutos;
- recusa por falta de consentimento `workout_data`.

A §18.4 fecha a saída: nunca silenciar. Uma nota de voz recusada sem resposta é exatamente o
silêncio que ela proíbe.

Isso não é novo nesta tarefa. A S02-T06 já entregou `enqueue_unsupported_media()` pelo mesmo motivo
— o plano da sprint pede "resposta fixa, redigida em `config/prompts/`" para foto e documento — e o
escopo da Sprint 02 diz, textualmente, que ela "não define o que o usuário recebe de resposta além
das mensagens fixas de degradação".

## Decisão

O `VoiceIngestion` decide qual das três respostas fixas se aplica e a entrega ao
`OutboundService` da S02-T06, que é o caminho único de saída durável. Três limites tornam isso
uma antecipação e não um segundo caminho:

1. **Não existe segunda saída.** O serviço nunca escreve `outbound_queue`, nunca fala com a API do
   canal e nunca entrega nada: ele passa um `OutboundBlock` por uma porta. O
   `test_the_service_never_writes_the_outbound_queue_itself` reprova a regressão.
2. **Ele não redige nada.** O texto vem de `config/prompts/*.md`, versionado, e no caso da
   inaudível é literalmente a string da §11.3. O que o serviço escolhe é *qual* constante, a partir
   de uma regra determinística — não é a decisão de conteúdo que a invariante 2 reserva ao
   `voice_agent`.
3. **A decisão é de canal-nenhum.** O bloco é `kind="text"` sem formatação, sem reação e sem botão;
   quem conhece `ChannelCaps` continua sendo só o adaptador de saída (AD-39).

A metade "enfileira" da invariante, portanto, já está respeitada hoje; a metade "decide" fica
antecipada por uma sprint.

## Consequências

- As respostas obrigatórias da §11.3 existem na fase em que a spec as exige, em vez de esperarem o
  grafo.
- Há um lugar fora do `voice_agent` que escolhe conteúdo visível ao usuário, e ele precisa sumir. A
  condição de saída está abaixo, não em "quando alguém lembrar".
- Enquanto durar, qualquer resposta nova nesse caminho é uma constante em `config/prompts/`: um
  texto gerado ali seria a violação de verdade.

## Condição de revisão

Expira na Sprint 03. Quando `voice_agent` e `deliver` entrarem, as três respostas viram blocos em
`state.outbound` decididos pelo `voice_agent`, e o `VoiceIngestion` passa a devolver apenas o
`VoiceStatus` — que ele já devolve hoje no `VoiceOutcome`, justamente para que essa troca seja
mecânica. O mesmo vale para o `enqueue_unsupported_media` da S02-T06.
