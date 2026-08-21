# Sprints

Como o trabalho do FitTrack é fatiado. A fonte da verdade continua sendo `doc/spec.md`; um sprint
não decide arquitetura, ele **executa** uma fatia dela e aponta as seções que cobre.

## Cadência

| Parâmetro | Valor |
| --- | --- |
| Duração | 2 semanas |
| Granularidade | Mais fina que as fases da §24 — a fase 1.0 sozinha tem 17 itens |
| Uma PR por tarefa | Branch `feat/`, `hotfix/` ou `doc/`, nome em inglês |
| Fim do sprint | Todos os critérios de saída verificados, não "o código foi escrito" |

## O que um sprint precisa ter

1. **Objetivo em uma frase** — o que passa a ser possível ao final.
2. **Escopo dentro e fora** — o "fora" importa tanto quanto o "dentro"; sem ele o sprint incha.
3. **Critérios de saída verificáveis** — comando que roda, número que se mede. "Funciona" não é
   critério.
4. **Tarefas mapeadas em PRs** — cada uma sai numa branch e vira um pull request.
5. **Seções da spec cobertas** — para a revisão da §5 do checklist ter contra o que comparar.
6. **Riscos** — o que pode dar errado e o plano B.

## Índice

| Sprint | Objetivo | Fase | Estado |
| --- | --- | --- | --- |
| [01](sprint-01-walking-skeleton.md) | Uma mensagem atravessa o sistema inteiro e volta | 1.0 | planejado |

## Definição de pronto

Uma tarefa está pronta quando, nesta ordem:

1. Os testes foram escritos **antes** e falharam pelo motivo certo.
2. A implementação passa nesses testes.
3. `gh pr checks` verde.
4. Comentários da revisão endereçados.
5. Confere com a seção da spec que ela implementa — divergência é bug ou ADR novo, nunca silêncio.
