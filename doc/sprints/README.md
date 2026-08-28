# Sprints do FitTrack

Este diretório transforma o roadmap da [spec](../spec.md) em incrementos executáveis de duas
semanas. A spec continua sendo a fonte da verdade para arquitetura e produto; uma sprint decide
apenas a ordem de implementação, os limites do incremento e como demonstrar que ele terminou.

## Regras de planejamento

- Cada sprint pertence a uma fase da §24 e referencia as seções da spec que implementa.
- Cada tarefa deve caber em um pull request revisável e produzir um resultado verificável.
- Dependências entre tarefas são explícitas. Trabalho independente pode ocorrer em paralelo.
- Toda mudança de comportamento segue TDD: teste falhando, implementação mínima, refatoração com a
  suíte verde.
- Uma tarefa só termina com `fmt`, `lint`, `typecheck` e testes aplicáveis verdes no CI.
- Uma divergência da spec exige correção da implementação ou um ADR; nunca uma decisão silenciosa.
- Credenciais reais, dados de usuário e operações de produção nunca fazem parte do critério de
  aceite local.

## Estados

| Estado | Significado |
| --- | --- |
| `planned` | Escopo e critérios definidos, trabalho ainda não iniciado |
| `in_progress` | Tarefa em implementação em uma branch própria |
| `blocked` | Impedimento externo documentado, com responsável e condição de desbloqueio |
| `done` | PR mergeado e todos os critérios de aceite comprovados |

O estado da sprint é derivado das tarefas. Uma sprint não fica `done` enquanto qualquer tarefa
obrigatória estiver em outro estado.

## Definition of Ready

Uma tarefa está pronta para começar quando:

1. objetivo, limites e referências da spec estão definidos;
2. dependências obrigatórias estão concluídas;
3. arquivos previstos e contratos afetados estão identificados;
4. o primeiro teste que deve falhar está descrito;
5. critérios de aceite podem ser verificados por comando ou observação objetiva.

## Definition of Done

Uma tarefa está concluída quando:

1. o ciclo red-green-refactor está registrado no PR;
2. implementação, testes e documentação estão no mesmo PR quando formam um único contrato;
3. `make fmt`, `make lint`, `make typecheck` e `make test` passam quando esses alvos existirem;
4. checks obrigatórios do GitHub estão verdes;
5. comentários de revisão foram endereçados;
6. a mudança foi revisada contra as seções citadas da spec;
7. suposições, limitações e trabalho adiado estão registrados.

## Fluxo de branches e PRs

- Código e infraestrutura: `feat/<short-description>`.
- Correções: `hotfix/<short-description>`.
- Documentação isolada: `doc/<short-description>`.
- Identificadores, comentários de código, commits e branches são sempre em inglês.
- Cada PR declara a tarefa da sprint, as seções da spec, os testes executados e qualquer desvio.
- O merge é sempre squash e só ocorre depois do checklist do `CLAUDE.md`.

## Índice

| Sprint | Fase | Objetivo | Estado |
| --- | --- | --- | --- |
| [Sprint 01 — Executable Foundation](01-foundation.md) | 1.0 | Fundação local, dados e segurança multi-tenant | `done` |
