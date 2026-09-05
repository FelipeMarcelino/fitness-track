# ADR-0014 — Achado de saúde e bloqueio são independentes

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-04 |
| Revisa | §§8.3 e 8.4 da spec |

## Contexto

O exemplo do guardrail representava o resultado como uma categoria exclusiva e mandava
qualquer resultado diferente de `PASS` diretamente ao `voice_agent`. A política de saúde
da §12.1, porém, exige registrar um relato de dor e manter possível a ingestão da série
no mesmo turno. Mais importante, relato de saúde e pedido bloqueante podem coexistir:
“me dói o ombro; qual remédio tomo?” não pode escolher somente uma das duas políticas.

## Decisão

O veredito tem dois eixos independentes: `health_report` é ausente ou contém o achado
persistível; `blocking_category` é ausente ou uma de `MEDICAL_ADVICE`, `EXTREME_DIET`,
`OFF_TOPIC` e `ABUSE`. `PASS` significa que ambos estão ausentes; `HEALTH_REPORT` deixa
de competir como categoria exclusiva.

Se houver `health_report`, o guardrail persiste o relato, atualiza `health_flag` e
acrescenta `health_notice` a `outbound` antes de decidir a rota. Sem categoria bloqueante,
segue para `router`, permitindo registrar a série do turno. Com categoria bloqueante,
acrescenta também o bloco semântico de recusa e segue diretamente para `voice`: o relato
de saúde permanece gravado, mas o pedido inseguro nunca alcança agentes de domínio.

O guardrail nunca diagnostica, prescreve ou altera a série. Ele registra o relato
cifrado, classifica o veredito e fixa a rota; a decisão clínica permanece fora do
produto.

## Consequências

Um relato de dor no mesmo turno de um treino não perde o treino nem deixa de receber o
aviso de segurança quando não há bloqueio. Um relato que também contém um pedido médico
continua registrado e recebe a recusa, sem atravessar o router. A topologia e os testes
precisam cobrir os quatro quadrantes dos dois eixos; tratar o veredito como uma enumeração
exclusiva passa a ser um bug de segurança e de integridade de dados.

## Condição de revisão

Reabrir somente se uma categoria nova não couber como achado persistível, bloqueio, ou
ambos. A proposta deve definir os dois eixos, rota, dados permitidos e crítico
determinístico antes da implementação.
