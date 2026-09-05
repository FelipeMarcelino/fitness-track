# ADR-0013 — RPE e RIR contraditórios

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-04 |
| Revisa | — (complementa §9.6 da spec) |

## Contexto

RPE e RIR se relacionam aproximadamente por `RIR ≈ 10 − RPE`, mas podem ser informados
explicitamente pelo usuário em combinação contraditória. A §9.6 dizia que o número
direto prevalece sobre a inferência textual, mas não definia o conflito entre dois
números diretos. Corrigir um deles silenciosamente transformaria uma inferência do
sistema em dado histórico do usuário.

## Decisão

Quando RPE e RIR forem ambos explícitos e contraditórios, o sistema preserva ambos,
marca a extração como de baixa confiança e não aplica precedência automática. A saída
crua registra a origem: `rpe_origin` é `explicit` ou `inferred`; `rir_origin` emitido
pelo LLM só pode ser `explicit`. Valor e origem sempre aparecem juntos, portanto uma
camada posterior sabe se os dois valores vieram de declarações explícitas sem tentar
reconstruir essa informação do texto. A regra de derivar RIR a partir de RPE só vale
quando RIR não foi informado explicitamente, roda em `domain/rpe.py`, fora do LLM, e o
DTO de persistência marca o resultado como `rir_origin="derived"`.

O fluxo pede esclarecimento somente quando a contradição tornar o registro inviável;
caso contrário, persiste o melhor registro com a sinalização de baixa confiança. A
precedência de número explícito sobre adjetivo continua válida e não autoriza alterar
outro número explícito.

## Consequências

O histórico preserva o que foi dito e deixa a incerteza visível para a confirmação em
texto, em vez de apresentar uma falsa coerência. Há casos de baixa confiança a mais,
mas eles são auditáveis e não contaminam a regra determinística com exceções implícitas.

## Condição de revisão

Reabrir se a interface passar a coletar uma confirmação explícita capaz de resolver o
conflito antes da persistência. A nova regra deverá registrar a escolha do usuário,
não inferir uma precedência retroativa.
