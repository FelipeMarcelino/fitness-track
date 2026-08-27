---
id: grounding
title: Fundamento
blocking: false
min_score: 4
universal: false
since_phase: "1.0"
---

## Critério

A recomendação cita princípio recuperado do RAG, ou é improviso?

Uma resposta pode ser numericamente fiel e ainda assim ser palpite. Esta rubrica pergunta se o
`rationale` se apoia em conhecimento recuperado — e, quando houver, se o `template_source`
correspondente aparece (§21.3).

## Escala

1. Afirma um princípio de treinamento falso ou desmentido pela literatura indexada.
2. Improviso: nenhum princípio é citado e a escolha não é justificada.
3. Justifica a escolha, mas só pelo histórico do usuário, sem princípio.
4. Cita princípio coerente com o material recuperado, sem identificar a fonte.
5. Cita princípio recuperado e identifica a fonte correspondente.
