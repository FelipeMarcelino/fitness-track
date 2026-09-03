# ADR-0008 — Coluna própria para a resposta fixa já enviada

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-03 |
| Revisa | — (acrescenta uma coluna ao `raw_message` da §5.2) |

## Contexto

Uma nota de voz recusada — inaudível, acima do teto de duração, ou sem consentimento
`workout_data` — recebe uma resposta fixa (§11.3). O drain só é reconhecido depois que o batch é
persistido e enfileirado (§17.3), então a mesma rajada é reprocessada quando qualquer um dos dois
falha, e sem um marcador durável o usuário é avisado duas vezes.

A primeira implementação usou `processed_at`, que já existia em `raw_message`. Foi um erro, e ele
apareceu na revisão cruzada: `save()` carimbava `processed_at` ao gravar uma **transcrição
bem-sucedida**, e `load()` lia esse mesmo campo como "já respondido". A consequência é pior do que
uma duplicata:

1. transcrição funciona e grava `processed_at`;
2. a persistência do batch falha, então o drain é retentado;
3. o consentimento `workout_data` é revogado nesse intervalo;
4. o ramo de consentimento chama `_refuse`, lê "já respondido", **suprime a resposta obrigatória** e
   ainda remove o item do batch.

O usuário fica sem resposta nenhuma. Não é higiene de schema: é supressão de resposta.

Manter `processed_at` com o significado novo também não serve. Quem termina uma mensagem é o grafo
da Sprint 03, e é ele que vai escrever essa coluna; uma resposta fixa chaveada nela passaria a ser
suprimida por contabilidade alheia, meses depois, num lugar onde ninguém procuraria.

## Decisão

`raw_message` ganha `answered_at TIMESTAMPTZ NULL`, escrita **apenas** por `mark_answered` e lida
apenas como "esta mensagem já teve sua resposta fixa". `processed_at` volta a não ser escrita por
este caminho e fica reservada para o grafo.

A migração `0005` é aditiva e a coluna é anulável, então ela se aplica a uma tabela em uso sem
reescrita. Não precisa de `GRANT`: a revisão inicial dá `UPDATE` em nível de tabela ao papel da
aplicação, o que cobre coluna nova.

## Consequências

- A §5.2 passa a ter uma coluna a mais em `raw_message` do que a spec descreve. Está registrado
  aqui em vez de virar divergência silenciosa, como o `CLAUDE.md` exige.
- Os dois fatos ficam separáveis: "transcrevemos" e "respondemos" podem ser consultados de forma
  independente, que é o que o coach proativo (§14) e a auditoria de entrega (§18.4) vão querer.
- Uma linha respondida antes desta migração não tem `answered_at` e pode receber uma segunda
  resposta fixa se o drain dela ainda estiver pendente. A janela é a do buffer (1h de TTL) e o
  efeito é uma mensagem repetida, não perdida — não vale um backfill.
- `tests/integration/test_stt_stores.py` fixa os dois sentidos: gravar transcrição não marca
  respondido, e carimbar `processed_at` por fora também não.

## Condição de revisão

Reabrir quando o grafo da Sprint 03 definir o ciclo de vida completo de `raw_message`. Se ele
introduzir uma máquina de estados explícita para a linha, `answered_at` provavelmente vira um
estado dela em vez de uma coluna própria.
