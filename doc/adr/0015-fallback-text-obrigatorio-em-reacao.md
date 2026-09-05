# ADR-0015 — `fallback_text` obrigatório em reação

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-04 |
| Revisa | §13.1 da spec |

## Contexto

O contrato de `VoiceOutput` reservava `text` para texto ou legenda. Entretanto, uma
reação pode falhar por emoji recusado ou por falta da mensagem-alvo, e o adaptador deve
degradar para texto sem decidir conteúdo. Sem texto já redigido pelo `voice_agent`, o
adaptador só poderia ficar silencioso ou inventar uma frase, ambas violações do caminho
único de saída.

## Decisão

`VoiceOutput` ganha `fallback_text`, obrigatório quando `mode="reaction"`. O
`voice_agent` o redige junto com a reação. O adaptador pode usá-lo somente depois de
uma falha de protocolo ou quando não houver mensagem-alvo para reagir; ele não cria,
edita ou escolhe uma mensagem alternativa.

Para todos os demais modos, `fallback_text` é nulo. O bloco convertido para a fila
mantém o texto como fallback de entrega, mas não o envia quando a reação foi aceita.

## Consequências

Uma degradação de reação preserva conteúdo e continua no único caminho
`voice_agent → deliver → outbound_queue`. O contrato fica um pouco mais amplo e os
testes de schema devem rejeitar uma reação sem fallback ou fallback em modo incompatível.

## Condição de revisão

Reabrir se todos os canais suportados passarem a oferecer reação confiável com garantia
de alvo. Enquanto qualquer adaptador puder recusar a reação, o fallback continua parte
do contrato de saída.
