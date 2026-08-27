---
id: profile_adherence
title: Aderência ao perfil
blocking: false
min_score: 4
universal: false
since_phase: "1.0"
---

## Critério

A resposta respeita objetivo, nível de experiência, equipamento disponível e lesões ativas do
perfil?

Julgamento gradual, não factual — por isso alimenta tendência (§21.2) em vez de bloquear. Uma queda
maior que 0,5 ponto em três rodadas abre issue.

## Escala

1. Contradiz o perfil: prescreve equipamento indisponível, ou carrega região com `health_report`
   aberto.
2. Ignora o perfil; a resposta serviria para qualquer usuário.
3. Respeita as restrições duras, mas não usa objetivo nem nível.
4. Usa objetivo e nível, com alguma escolha genérica remanescente.
5. Objetivo, nível, equipamento e lesões ativas visivelmente moldam a resposta.
