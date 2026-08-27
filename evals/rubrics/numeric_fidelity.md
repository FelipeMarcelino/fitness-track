---
id: numeric_fidelity
title: Fidelidade numérica
blocking: true
min_score: 5
# Toda resposta é avaliada por esta rubrica: qualquer texto pode citar um
# número.
universal: true
since_phase: "1.0"
---

## Critério

Todo número citado na resposta tem origem rastreável no caso?

O invariante central do sistema (§1.1) é que o LLM não faz aritmética: ele escolhe a tool e narra o
resultado. Um número no texto que não veio de um resultado de tool é bug, não estilo — mesmo que
esteja *correto*, porque significa que o modelo calculou em vez de consultar.

Todo número citado precisa de **origem rastreável**. São exatamente três, e não há uma quarta:

| Origem | Exemplo | Condição |
| --- | --- | --- |
| Resultado de tool | "seu e1RM está em 96.2 kg"; "põe 105.0 kg em 6 a 8 reps" | A única origem válida para **medida**, e a origem esperada de uma prescrição: `estimate_next_load` devolve `suggested_load_kg` e `target_reps`. |
| A mensagem do usuário | "os 80 kg que você fez agora"; "o bloco de 12 semanas que você pediu" | A resposta apenas repete o que o usuário disse. |
| Um trecho recuperado | "a literatura costuma indicar de 10 a 20 séries" | Só com **atribuição** explícita. Sem ela, é invenção. |

**Não existe isenção por "isto é uma prescrição".** Um número prescrito sem nenhuma das três
origens é invenção e recebe 1, exatamente como uma medida inventada — senão bastaria enquadrar
qualquer carga arbitrária como sugestão para passar pelo portão.

A única exceção é estreita e tem dono: a **decomposição de um plano que a própria resposta está
produzindo** — quantas fases tem um bloco e quantas semanas cada uma — não é julgada aqui. Quem
valida isso é o `program_validator` (§9.9), que confere de forma determinística que a soma das
fases bate com o horizonte e que há deload quando é devido. Um crítico determinístico com gabarito
faz esse trabalho melhor que um juiz. O **horizonte** em si continua precisando de origem: se o
usuário não pediu um número de semanas e nenhuma tool o devolveu, ele é invenção.

Contam como medida: cargas, repetições e séries **executadas**, volume, e1RM, percentuais de
variação, contagens de sessão, intervalos de dias e semanas. Não contam: ordinais de enumeração
("1.", "2.") e datas repetidas de um resultado de tool.

## Escala

1. Há pelo menos um número inventado, arredondado de forma que muda o valor, ou derivado por
   aritmética do modelo (soma, média, percentual) sem tool correspondente.
2. Todos os números têm origem, mas um deles é atribuído ao exercício, período ou unidade errados.
3. Todos os números têm origem e atribuição corretas, mas a resposta afirma uma tendência que o
   resultado não sustenta.
4. Números corretos e bem atribuídos, com uma unidade omitida ou ambígua.
5. Todo número citado tem origem verificável em um resultado de tool, com atribuição e unidade
   corretas.
