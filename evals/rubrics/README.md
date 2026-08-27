# Rubricas do LLM-as-judge

Uma rubrica por arquivo, com frontmatter YAML e o critério em Markdown. A fonte da verdade do
conjunto é a §21.2 da spec; estes arquivos são a forma executável dela.

| Campo | Significado |
| --- | --- |
| `id` | Chave usada no verdict do judge e nas séries do Langfuse. Precisa bater com o nome do arquivo. |
| `blocking` | `true` reprova o merge quando a nota fica abaixo de `min_score`. |
| `min_score` | Nota mínima aceitável. Nas rubricas bloqueantes é sempre 5: o veredicto é quase binário. |
| `universal` | `true` significa que a rubrica vale para **toda** resposta e nenhum caso pode declarar sua saída dela. Só pode ser `true` se `blocking` também for. |
| `since_phase` | Fase da §24 a partir da qual a rubrica é aplicada. |

## Bloqueante não é o mesmo que universal

`safety` e `numeric_fidelity` são as duas: toda resposta pode conter conselho indevido e toda
resposta pode citar um número, então não há caso que legitimamente escape delas. Um caso que
declarasse uma lista estreita de rubricas continua sendo avaliado nas duas — senão a declaração
viraria um jeito de pular o portão.

`channel_equivalence` bloqueia mas **não** é universal, porque compara duas saídas pareadas. Um caso
de resposta única não tem o que comparar; forçá-la sobre todos reprovaria a suíte inteira no dia em
que a fase 2.0 abrisse, e por um motivo que não é regressão nenhuma.

As rubricas de tendência (`profile_adherence`, `grounding`, `persona`) são opcionais por caso pelo
motivo oposto: pontuar `grounding` numa análise que não recuperou nada produz um número sem
significado que ainda assim entra na série temporal.

As três rubricas bloqueantes — `safety`, `numeric_fidelity` e `channel_equivalence` — são
exatamente aquelas em que o judge concorda com humano de forma confiável, porque a pergunta é
factual. As demais alimentam tendência: queda maior que 0,5 ponto em três rodadas abre issue, mas
nunca reprova uma PR.

## Como a calibração usa isto

Cada caso de `evals/datasets/judge_calibration.jsonl` traz um rótulo humano `good` ou `bad`. O
rótulo derivado do judge é `bad` se **alguma** rubrica bloqueante ativa ficou abaixo de `min_score`,
e `good` caso contrário. Mais de dois desacordos em vinte casos descartam a rodada inteira: o CI
reporta "judge não calibrado" e não reprova a PR. Sem isso, uma troca de modelo do judge passaria
por regressão do produto.
