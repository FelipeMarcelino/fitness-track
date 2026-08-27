# ADR-0002 — Portão do LLM-as-judge suspenso na fundação

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-08-27 |
| Revisa | — (não altera nenhuma decisão da §2; suspende temporariamente um portão da §21.4) |

## Contexto

A §21.2 define o LLM-as-judge com veto assimétrico: `safety` e `numeric_fidelity` abaixo de 5
reprovam o merge, as demais rubricas viram tendência. O AD-33 é explícito quanto ao *quando*: o
judge roda **desde a primeira PR de código**, porque esperar até haver "código suficiente" acumula
regressão sem que se saiba qual mudança a causou.

A S01-T01 implementou o runner inteiro — rubricas versionadas, 20 casos de calibração com nota
humana, amostra de 40, política de bloqueio e de descarte por calibração. A revisão do Codex apontou,
corretamente, que um job que sai com 0 por falta de credencial deixa um *required check* verde sobre
um diff que ninguém avaliou. O runner passou a ter dois modos: tolerante (`make eval-judge`, uso
local) e estrito (`--backend anthropic`, o que o CI chama), e o estrito sai com 1 quando não há
credencial.

Com o secret `ANTHROPIC_API_KEY` configurado no repositório, a chamada real devolveu:

```
anthropic.BadRequestError: Error code: 400 — 'Your credit balance is too low
to access the Anthropic API.'
```

A credencial existe e é válida; a conta não tem saldo. Não é um problema de configuração e não há
caminho técnico que o contorne sem mentir sobre o resultado.

## Decisão

O job `LLM-as-judge` roda com `continue-on-error: true` até a conta ter saldo.

O que **não** muda, e é o que impede isto de virar uma passada silenciosa:

- O modo estrito continua sendo o que o CI chama. Quando o judge roda, ele bloqueia de verdade.
- O job continua aparecendo vermelho no PR quando falha. `continue-on-error` impede que ele reprove
  o merge; não o esconde.
- A suíte de testes do próprio portão continua bloqueante. `tests/unit/test_judge_gates.py` e
  `test_judge_calibration.py` provam a política inteira com verdicts gravados, sem rede e sem
  credencial — uma regressão *na política* reprova hoje, mesmo sem o judge rodar.
- Os datasets continuam versionados e testados: 20 casos de calibração balanceados, 40 de amostra,
  contrato verificado por `test_judge_datasets.py`.

Alternativas descartadas:

| Alternativa | Por que não |
| --- | --- |
| Voltar o CI ao modo tolerante | É exatamente o achado P1 do Codex. Um check verde diria que segurança e fidelidade numérica foram avaliadas |
| Parar a sprint até haver saldo | O bloqueio é de faturamento e a fundação inteira não depende dele |
| Trocar o judge por um modelo mais barato | A §7.2 é explícita: o `JUDGE` não tem primário de propósito, e um juiz que compartilha modo de falha com o avaliado não é juiz |
| Remover o job | Apagaria o débito em vez de registrá-lo |

## Consequências

**Ruim, e precisa ser dito.** Entre este ADR e a reativação, nenhuma PR é avaliada por segurança nem
por fidelidade numérica ao vivo. É precisamente a regressão silenciosa que o AD-33 queria evitar, e
o único consolo é que na fundação ainda não há agente cuja saída pudesse regredir — a janela de
exposição é a menor que jamais será.

**Consequência prática:** toda PR que entrar com o portão suspenso e que toque `config/prompts/`,
`src/fittrack/agents/`, `src/fittrack/graph/` ou `evals/` deve ser reavaliada pelo judge assim que
ele voltar, antes da primeira PR que dependa dela. Os verdicts de uma rodada podem ser gravados com
`--out` e reavaliados depois com `--backend replay`, o que torna isso barato.

## Condição de revisão

A conta Anthropic passar a ter saldo. A reversão é uma linha:

```diff
   judge:
     name: LLM-as-judge
-    continue-on-error: true   # ADR-0002
```

Rodar `python -m evals.run_judge --backend anthropic` uma vez à mão confirma antes de mexer no CI.
Se a calibração reprovar na volta (mais de 2 dos 20 casos em desacordo com a nota humana), o
problema é o judge e não o produto, e a rodada é descartada por desenho — ver §21.2.
