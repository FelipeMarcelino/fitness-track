---
id: numeric_fidelity
title: Fidelidade numérica
blocking: true
min_score: 5
since_phase: "1.0"
---

## Critério

Todo número citado na resposta aparece em algum resultado de tool fornecido no caso?

O invariante central do sistema (§1.1) é que o LLM não faz aritmética: ele escolhe a tool e narra o
resultado. Um número no texto que não veio de um resultado de tool é bug, não estilo — mesmo que
esteja *correto*, porque significa que o modelo calculou em vez de consultar.

Contam como números: cargas, repetições, séries, volume, e1RM, percentuais, contagens de sessão,
intervalos de dias e semanas. Não contam: números que o próprio usuário disse na mensagem e que a
resposta apenas repete, e ordinais de enumeração ("1.", "2.").

Esta rubrica é redundante com o `numeric_critic` (§9.9) **de propósito**: o crítico protege
produção, o judge detecta a regressão de prompt que faria o crítico começar a vetar.

## Escala

1. Há pelo menos um número inventado, arredondado de forma que muda o valor, ou derivado por
   aritmética do modelo (soma, média, percentual) sem tool correspondente.
2. Todos os números têm origem, mas um deles é atribuído ao exercício, período ou unidade errados.
3. Todos os números têm origem e atribuição corretas, mas a resposta afirma uma tendência que o
   resultado não sustenta.
4. Números corretos e bem atribuídos, com uma unidade omitida ou ambígua.
5. Todo número citado tem origem verificável em um resultado de tool, com atribuição e unidade
   corretas.
