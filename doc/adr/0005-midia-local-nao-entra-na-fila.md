# ADR-0005 — Mídia local não entra na fila

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-02 |
| Revisa | — (fecha uma lacuna entre as §§18.2/18.4 e a invariante 5) |

## Contexto

O contrato de canal representa mídia de saída com `OutboundBlock.media_path`. O Telegram aceita os
bytes no mesmo envio, então um caminho local é suficiente enquanto download e envio acontecem no
mesmo worker. A fila de saída, porém, persiste o bloco e qualquer um dos quatro workers pode consumir
a linha ou repetir a tentativa depois de um restart.

Os containers têm `/tmp` privado, efêmero e limitado a 256 MB. Persistir
`/tmp/fittrack-media-...` no Postgres não persiste o arquivo: outro worker recebe uma referência que
não existe, e nem o mesmo worker a conserva após reiniciar. Isso viola a invariante 5 do
`CLAUDE.md`, segundo a qual workers são stateless e estado vive em Postgres, Redis ou Qdrant.

O repositório não possui object storage, volume compartilhado ou uma representação durável de blob
para mídia de saída. Criar apenas a interface sem a infraestrutura manteria a mesma falha escondida.

## Decisão

Uma resposta com `media_path` local não pode entrar em `outbound_queue`. Tanto o serviço de saída
quanto o store PostgreSQL recusam o enqueue com erro explícito antes de persistir qualquer linha. O
payload deixa de serializar caminhos locais; o decoder conserva compatibilidade com linhas antigas.

Um bloco de mídia entregue diretamente no mesmo worker continua permitido. Depois de um resultado
terminal — envio confirmado ou dead letter — seu arquivo temporário é removido. Uma tentativa que
será repetida mantém o arquivo durante a vida do worker, mas não pode ser durabilizada na fila.

## Consequências

- Nenhum worker recebe pela fila um caminho pertencente ao tmpfs de outra réplica.
- Produzir mídia para envio durável falha alto no enqueue em vez de criar trabalho impossível.
- A fase atual não oferece envio assíncrono/retry de mídia; texto, reações, botões e templates seguem
  normalmente.
- A remoção terminal evita acumular arquivos temporários nos fluxos de entrega direta.
- O campo continua no contrato de canal porque adapters ainda precisam de um caminho para um envio
  inline no mesmo processo, e porque removê-lo seria uma mudança maior que o problema da fila.

## Condição de revisão

Reabrir quando existir armazenamento de blobs compartilhado e durável, com referência opaca,
expiração e exclusão definidas. Nesse momento a fila deve persistir a referência, nunca o caminho do
filesystem de um container, e os testes devem provar retry após troca de worker e restart.
