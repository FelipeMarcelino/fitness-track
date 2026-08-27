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

Todo número citado na resposta aparece em algum resultado de tool fornecido no caso?

O invariante central do sistema (§1.1) é que o LLM não faz aritmética: ele escolhe a tool e narra o
resultado. Um número no texto que não veio de um resultado de tool é bug, não estilo — mesmo que
esteja *correto*, porque significa que o modelo calculou em vez de consultar.

O que esta rubrica julga é **medida**, não prescrição. O invariante da §1.1 fala de métrica —
volume, e1RM, tendência, frequência — e é sobre ela que o veto existe. Três origens são legítimas, e
qualquer outra é invenção:

| Origem | Exemplo | Condição |
| --- | --- | --- |
| Resultado de tool | "seu e1RM está em 96.2 kg" | A única origem válida para **medida**. |
| A mensagem do usuário | "os 80 kg que você fez agora" | A resposta apenas repete o que o usuário disse. |
| A própria prescrição | "3 séries de 8", "bloco de 12 semanas em 3 fases" | Só quando o número é **prescrito**, e o texto o apresenta como tal — não como medida. |

Um número recuperado do RAG **não** é uma dessas: se a resposta cita uma faixa da literatura, ela
tem de atribuí-la ("a literatura costuma indicar…"), e ainda assim a rubrica a trata como
prescrição, nunca como medida do usuário.

Contam como medida: cargas, repetições e séries **executadas**, volume, e1RM, percentuais de
variação, contagens de sessão, intervalos de dias e semanas. Não contam: ordinais de enumeração
("1.", "2.") e datas repetidas de um resultado de tool.

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
