# ADR-0012 — Proveniência plural e imutável da série

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-04 |
| Revisa | — (complementa §§5.2 e 9.5 da spec) |

## Contexto

Uma rajada pode formar uma única série a partir de vários fragmentos: por exemplo, `"supino"`,
`"80 kg"` e `"8 reps"`. O schema atual de `exercise_set` tem apenas `source_message_id` e
`source_text` escalares. Escolher uma das mensagens como fonte atribui a ela campos que ela não
contém; concatenar o `clean_text` perde os literais e a posição de origem. As duas leituras impedem
auditoria fiel da extração e não constituem uma chave de idempotência estável.

## Decisão

A saída crua do extrator referencia a origem por spans, nunca por texto reconstruído:

```python
class SourceSpan(BaseModel):
    fragment_index: int
    start: int
    end: int

class ExtractedSet(BaseModel):
    source_spans: list[SourceSpan]  # não vazia, ordenada
```

Depois do Pydantic, código determinístico confere que cada índice existe, que `0 <= start < end <=
len(raw_fragment.text)`, que `start`/`end` são offsets de código Python no literal, que os spans estão
em ordem canônica sem sobreposição dentro da série e que o recorte é literal. O fragmento confiável
inclui o `raw_message_id` já conhecido pelo worker; o LLM nunca escolhe uma linha de banco. Spans podem
se repetir em séries diferentes quando o mesmo texto
efetivamente sustenta ambas.

A T12 cria `exercise_set_source`, relação append-only com `exercise_set_id`, `tenant_id`, posição,
`raw_message_id`, `start` e `end`. A FK é tenant-qualificada para `raw_message` (a migração também
declara a unicidade necessária de `(id, tenant_id)`), para que uma proveniência nunca atravesse
tenants. A tabela não replica o literal em claro: o texto permanece cifrado em `raw_message.payload`
e é recuperado pelo span apenas quando uma auditoria autorizada precisa dele.

`exercise_set.source_text` deixa de ser a fonte de verdade. Ele recebe exclusivamente o recorte literal
quando há um único span; com múltiplos spans fica `NULL`. Nunca recebe `clean_text`, uma concatenação
normalizada ou um fragmento escolhido arbitrariamente. A chave de idempotência passa a usar
`provenance_hash = SHA-256` da sequência canônica `(raw_message_id, start, end)`, junto de sessão,
exercício e índice da série; `source_message_id` sai desse índice.

## Consequências

Uma série montada numa rajada preserva todas as evidências sem duplicar dado sensível em claro. Em
troca, a T05 deve expor `raw_message_id` no contrato interno de `raw_fragments`, e a T12 ganha uma
tabela, uma FK composta e uma migração de índice. Os testes cobrem os três fragmentos, offsets
inválidos/fora de ordem, FK cruzada entre tenants, reprocessamento e a ausência de texto sintético.

## Condição de revisão

Reabrir se o processamento deixar de preservar `raw_message` durante a vida auditável de uma
`exercise_set`, pois então os spans precisariam de uma retenção cifrada própria.
