# ADR-0014 — `HEALTH_REPORT` segue para o router

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-04 |
| Revisa | §§8.3 e 8.4 da spec |

## Contexto

O exemplo do guardrail mandava qualquer resultado diferente de `PASS` diretamente ao
`voice_agent`. A política de saúde da §12.1, porém, exige registrar a série quando a
mesma mensagem também relata dor. Pular o router impede a ingestão e descarta um
registro que a invariante de falhar registrando exige preservar.

## Decisão

`HEALTH_REPORT` atualiza `health_flag`, acrescenta o aviso de saúde a `outbound` e
segue para o `router`. Assim o plano pode incluir ingestão e a saída final combina a
confirmação com orientação acolhedora. As demais categorias bloqueantes
(`MEDICAL_ADVICE`, `EXTREME_DIET`, `OFF_TOPIC` e `ABUSE`) seguem diretamente para
`voice` e não acionam agentes de domínio.

O guardrail nunca diagnostica, prescreve ou altera a série. Ele registra o relato
cifrado, classifica o veredito e fixa a rota; a decisão clínica permanece fora do
produto.

## Consequências

Um relato de dor no mesmo turno de um treino não perde o treino nem deixa de receber o
aviso de segurança. A topologia e os testes precisam distinguir `HEALTH_REPORT` dos
demais não-`PASS`; tratar todos como bloqueantes passa a ser um bug de segurança e de
integridade de dados.

## Condição de revisão

Reabrir somente se uma categoria nova tiver simultaneamente requisito de segurança e
requisito de persistência que não caiba nessa separação. A proposta deve definir rota,
dados permitidos e crítico determinístico antes da implementação.
