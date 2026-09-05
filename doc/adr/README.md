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
| [0002](0002-portao-do-judge-suspenso.md) | Portão do LLM-as-judge suspenso na fundação | substituído por ADR-0004 | — |
| [0003](0003-escopo-de-tenant-dentro-da-chave-estrangeira.md) | Escopo de tenant dentro da chave estrangeira | aceito | — |
| [0004](0004-openai-como-provider-do-judge.md) | OpenAI como provider do LLM-as-judge | aceito | AD-19 (somente JUDGE) |
| [0005](0005-midia-local-nao-entra-na-fila.md) | Mídia local não entra na fila | aceito | — |
| [0006](0006-voz-longa-continua-sendo-voz.md) | Áudio longo demais continua sendo `voice` | aceito | — |
| [0007](0007-stt-fora-do-llm-gateway.md) | STT fora do `LLMGateway`, configurado em `models.yaml` | aceito | — |
| [0008](0008-coluna-propria-para-resposta-fixa.md) | Coluna própria para a resposta fixa já enviada | aceito | — |
| [0009](0009-respostas-fixas-antes-do-grafo.md) | Respostas fixas da §11.3 antes de o `voice_agent` existir | aceito, expira na Sprint 03 | — |
| [0011](0011-sdks-nativos-na-camada-de-provider.md) | SDKs nativos na camada de provider | aceito | §7.4 (linha "SDK LangChain") |
| [0012](0012-proveniencia-plural-da-serie.md) | Proveniência plural e imutável da série | aceito | — |
| [0018](0018-conversao-deterministica-de-unidades-fora-do-llm.md) | Conversão determinística de unidades fora do LLM | aceito | — |

> ⚠️ **ADR-0001 está referenciado mas ausente da árvore.** O `CLAUDE.md` e a §2 da spec apontam para
> `doc/adr/0001-groq-como-provider-primario.md`, e o arquivo não existe. O conteúdo da decisão está
> descrito na §7.2 e no Apêndice A (Groq primário e Anthropic como fallback dos papéis de produto),
> mas o racional que a substituiu do AD-19 não foi
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
