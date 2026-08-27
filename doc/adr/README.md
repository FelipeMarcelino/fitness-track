# Decisões de arquitetura

As 43 decisões da §2 da [spec](../spec.md) são a base e estão fechadas. Este diretório registra o
que mudou **depois** delas: uma decisão nova, ou a revisão de uma antiga com evidência que não
existia quando ela foi tomada.

Um ADR não substitui a spec. Quando um ADR revisa uma decisão da §2, a linha correspondente da
tabela passa a apontar para ele, e a spec continua sendo a fonte da verdade sobre o *estado atual* —
o ADR explica *como se chegou nele*.

> **Atenção ao número.** A spec v2.0 renumerou a tabela §2 (era AD-01..AD-36, hoje é AD-01..AD-43).
> Um `AD-NN` citado fora da spec pode apontar para outra decisão. A §2 é a referência; o número
> solto não é.

## Índice

| ADR | Título | Estado | Revisa |
| --- | --- | --- | --- |
| 0001 | Groq como provider primário | aceito | AD-19 |
| [0002](0002-portao-do-judge-suspenso.md) | Portão do LLM-as-judge suspenso na fundação | aceito | — |

> ⚠️ **ADR-0001 está referenciado mas ausente da árvore.** O `CLAUDE.md` e a §2 da spec apontam para
> `doc/adr/0001-groq-como-provider-primario.md`, e o arquivo não existe. O conteúdo da decisão está
> descrito na §7.2 e no Apêndice A (Groq primário, Anthropic como fallback de todo papel e único
> provider do `JUDGE`, OpenAI só para embeddings), mas o racional que a substituiu do AD-19 não foi
> escrito em lugar nenhum. Escrevê-lo a partir de inferência seria inventar a justificativa, então
> fica registrado aqui como lacuna a preencher por quem tomou a decisão.

## Formato

Um arquivo por decisão, `NNNN-titulo-em-kebab-case.md`, com:

```markdown
# ADR-NNNN — Título

| Campo | Valor |
| --- | --- |
| Estado | proposto / aceito / substituído por ADR-XXXX |
| Data | AAAA-MM-DD |
| Revisa | AD-NN da §2, ou — |

## Contexto
O que era verdade quando a decisão anterior foi tomada, e o que mudou.

## Decisão
O que se passa a fazer.

## Consequências
O que fica melhor, o que fica pior, e o que passa a exigir atenção.

## Condição de revisão
O fato observável que faria valer a pena reabrir isto.
```

A **condição de revisão** é o campo que costuma faltar e o que mais economiza tempo depois. Sem ela,
um ADR vira decisão permanente por inércia; com ela, sabe-se de antemão o que observar.
