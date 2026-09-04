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

A primeira versão desta decisão também errava em três pontos que só apareceram na revisão do PR, e
que interagem entre si — corrigi-los em qualquer ordem que não seja "todos juntos" reintroduz um dos
outros três:

1. **O extrator não pode enxergar o que teria de apontar.** `agents/extraction.py` recebe apenas o
   `NormalizedTurn` (§9.3, "nenhum agente vê o texto bruto"). Pedir ao LLM um offset de caractere
   Python no `raw_fragment.text` original pede a ele apontar para um texto que ele nunca leu — e que
   pode nem existir mais daquela forma, porque o `conversation_normalizer` corrige STT
   ("super no reto" → "supino reto") antes de o extrator ver qualquer coisa. Um span "adivinhado"
   contra um literal nunca visto não é verificável; só parece verificável.
2. **O span aponta para a coluna errada em áudio.** `raw_fragment.text` de um fragmento
   `was_audio=true` é a *transcrição*, cifrada em `raw_message.transcript`. `raw_message.payload`
   cifra o evento original do canal (para o Telegram, o update inteiro), não o texto falado. Um
   registro de proveniência que sempre aponta para `payload` está estruturalmente incapaz de
   recuperar a evidência de uma série ditada por voz.
3. **`set_index` na chave de idempotência não sobrevive a um commit sem checkpoint.** A T12 deriva
   `set_index` do conteúdo atual da sessão ("próximo índice livre"). Se o INSERT committa mas o
   checkpoint do grafo não, o retry do batch enxerga a sessão já com a linha anterior — o próximo
   índice livre avançou — e calcula um `set_index` **diferente** para a mesma extração. A chave
   antiga `(session_id, exercise_id, set_index, source_message_id)` não colide contra si mesma, e o
   retry duplica a série.

## Decisão

### Spans nunca nascem de um LLM apontando para texto que ele não viu

A responsabilidade de ligar texto normalizado a texto bruto sai do LLM e vira um passo determinístico
dentro do nó do normalizador — o único lugar do grafo que tem, ao mesmo tempo, o fragmento bruto (via
`raw_fragments` do `GraphState`, nunca exposto a um prompt) e o texto reescrito que o LLM acabou de
devolver.

```python
# domain/provenance.py — contrato compartilhado por T08, T09, T12 e T23

class SourceSpan(BaseModel):
    fragment_index: int
    start: int
    end: int
    source_field: Literal["payload", "transcript"]  # qual coluna cifrada o span indexa

def align_segment_spans(
    segment: TurnSegment, raw_fragments: list[RawFragment]
) -> list[SourceSpan]:
    """Determinístico. Para cada fragmento citado em segment.source_fragments, localiza o melhor
    trecho contíguo de raw_fragment.text (payload) ou raw_fragment.transcript (was_audio=True) que
    sustenta segment.text, via difflib.SequenceMatcher (stdlib, sem heurística de LLM). Abaixo do
    limiar de similaridade fixo (0.6, constante versionada — não ajustada por caso), a correção do
    normalizer foi grande demais para localizar um trecho com confiança: cai para o span do
    fragmento inteiro, que ainda é evidência literal (não sintética), só que de granularidade maior.
    Nunca levanta: sempre devolve 0 <= start < end <= len(texto-fonte)."""

def resolve_spans(
    segment_indices: list[int], segments: list[TurnSegment]
) -> list[SourceSpan]:
    """Determinístico. Um agente a jusante (extractor, guardrail) não cita span nenhum — cita
    `segment_indices`, os únicos números que ele realmente observou, porque são índices no
    `NormalizedTurn.segments` que o prompt recebeu por inteiro. Esta função só junta os
    `SourceSpan` que `align_segment_spans` já calculou para cada segmento citado, remove
    duplicata e ordena de forma canônica (fragment_index, start). Rejeita índice inexistente."""
```

`align_segment_spans` roda **uma vez**, em `graph/nodes/normalizer.py`, logo depois da chamada ao
`NORMALIZER` e antes de o estado ser escrito: cada `TurnSegment` sai do nó já com `source_spans`
preenchido pelo código, nunca pelo LLM. É por isso que o problema 2 (coluna errada) se resolve no
mesmo lugar que o problema 1: quem escolhe `payload` vs. `transcript` é o mesmo código que já sabe,
por `raw_fragment.was_audio`, qual coluna decifrar — a escolha nunca é um campo que o LLM preenche.

A jusante, nem o `extraction_agent` (T09) nem o `guardrail_agent` (T23) emitem `SourceSpan`. Os dois
emitem `source_segments: list[int]`, índices no `NormalizedTurn.segments` que o prompt já contém por
inteiro — o LLM aponta para algo que ele efetivamente leu, e nunca faz aritmética de offset. Código
determinístico (`resolve_spans`, chamado logo após a validação Pydantic de cada saída, antes de tocar
o banco) resolve `source_segments` para os `SourceSpan` que `align_segment_spans` já calculou:

```python
class ExtractedSet(BaseModel):
    source_segments: list[int]  # não vazia; índices em NormalizedTurn.segments
    # source_spans NÃO existe neste schema — é derivado, não emitido pelo LLM
```

Depois da resolução, código determinístico confere que cada índice de segmento existe, que os spans
resultantes satisfazem `0 <= start < end <= len(texto-fonte)`, que estão em ordem canônica sem
sobreposição dentro da série, e que o recorte é literal. O `raw_message_id` de cada span vem de
`raw_fragments[span.fragment_index].raw_message_id` (contrato que a T05 já expõe); o LLM nunca
escolhe uma linha de banco, nem antes nem depois desta revisão. Spans podem se repetir em séries
diferentes quando o mesmo texto efetivamente sustenta ambas.

O `guardrail_agent` usa exatamente o mesmo mecanismo para `verbatim_spans` (T23): cita
`source_segments`, código resolve para `SourceSpan`, decifra `payload` ou `transcript` conforme
`source_field` de cada span, recorta e concatena na ordem canônica — esse é o único texto que vira
`health_report.verbatim` antes de cifrar. Nunca é `clean_text`, e nunca é uma reprodução literal
escrita pelo próprio LLM.

### A tabela de proveniência, tenant-qualificada nas duas FKs

A T12 cria `exercise_set_source`, relação append-only:

```sql
-- Chave candidata que a FK composta abaixo exige (mesmo padrão do ADR-0003 para `exercise`,
-- mais simples aqui porque exercise_set.tenant_id nunca é NULL — não há "série global").
ALTER TABLE exercise_set  ADD CONSTRAINT uq_exercise_set_id_tenant UNIQUE (id, tenant_id);
ALTER TABLE raw_message   ADD CONSTRAINT uq_raw_message_id_tenant  UNIQUE (id, tenant_id);

CREATE TABLE exercise_set_source (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    exercise_set_id BIGINT NOT NULL,
    raw_message_id  BIGINT,                 -- nullable de propósito, ver retenção abaixo
    position        SMALLINT NOT NULL,       -- ordem canônica do span dentro da série (0-based)
    source_field    TEXT NOT NULL,           -- 'payload' | 'transcript'
    fragment_index  SMALLINT NOT NULL,
    start_offset    INTEGER NOT NULL,
    end_offset      INTEGER NOT NULL,
    -- Denormalizado a partir de raw_message no momento do INSERT — sobrevive à purga de
    -- raw_message (ver "Retenção" abaixo), para que a proveniência estrutural não desapareça
    -- inteira junto do literal.
    channel         channel_kind NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_source_field CHECK (source_field IN ('payload', 'transcript')),
    CONSTRAINT ck_span_bounds  CHECK (start_offset >= 0 AND start_offset < end_offset),
    UNIQUE (exercise_set_id, position),

    -- Tenant-qualificada: um FK comum para exercise_set(id) deixaria o tenant A inserir
    -- proveniência apontando para uma série do tenant B sem nunca conseguir lê-la — RLS não
    -- valida a linha referenciada, só a linha própria (mesma classe de bug do ADR-0003).
    FOREIGN KEY (exercise_set_id, tenant_id)
        REFERENCES exercise_set (id, tenant_id) ON DELETE CASCADE,

    -- Composta pelo mesmo motivo. SET NULL (raw_message_id) — sintaxe de coluna específica,
    -- PG15+ — zera só a referência ao raw_message quando ele é purgado; tenant_id, que também
    -- participa da FK, fica intocado, porque ele identifica o DONO desta linha de proveniência,
    -- não a mensagem apagada. Diferente do MATCH SIMPLE que o ADR-0003 teve de fechar: ali um
    -- NULL era gravável pela aplicação e virava brecha; aqui NULL só é alcançado por este próprio
    -- ON DELETE, nunca por um INSERT da aplicação, então não reabre o oráculo de existência.
    FOREIGN KEY (raw_message_id, tenant_id)
        REFERENCES raw_message (id, tenant_id) ON DELETE SET NULL (raw_message_id)
);
CREATE INDEX ix_source_tenant      ON exercise_set_source(tenant_id);
CREATE INDEX ix_source_raw_message ON exercise_set_source(raw_message_id)
    WHERE raw_message_id IS NOT NULL;
```

A tabela não replica o literal em claro: o texto permanece cifrado em `raw_message.payload` ou
`raw_message.transcript`, e é recuperado pelo span apenas quando uma auditoria autorizada precisa
dele — enquanto `raw_message` ainda existir.

### Retenção: o FK para `raw_message` não pode ser incondicional

`raw_message.payload`/`.transcript` têm retenção de 90 dias (§19.5); `exercise_set` vive enquanto a
conta existir. Um FK comum quebraria a purga (viola FK) ou, com `CASCADE`, apagaria proveniência
auditável de séries que continuam existindo — perdendo exatamente o que este ADR promete. Nenhuma das
duas é aceitável.

A decisão é `ON DELETE SET NULL (raw_message_id)` mais o *snapshot* denormalizado (`channel`,
`occurred_at`) já declarado no `CREATE TABLE` acima. Depois dos 90 dias, uma linha de
`exercise_set_source` perde a capacidade de recuperar o literal — que é o comportamento **correto**:
a política de retenção existe para que o texto bruto deixe de existir — mas preserva "esta série veio
de uma mensagem de voz, recebida em tal data, neste canal", que é auditoria estrutural e não depende
do conteúdo apagado. `start_offset`/`end_offset`/`fragment_index` continuam na linha depois do purge;
ficam inertes (não referenciam mais nada decifrável), o que é inofensivo — não são números que
concedem acesso a nada.

`exercise_set.source_text` deixa de ser a fonte de verdade. Ele recebe exclusivamente o recorte
literal quando há um único span; com múltiplos spans fica `NULL`. Nunca recebe `clean_text`, uma
concatenação normalizada ou um fragmento escolhido arbitrariamente.

### Idempotência: nunca uma coluna que muda com o retry

```sql
ALTER TABLE exercise_set
    ADD COLUMN provenance_hash   BYTEA   NOT NULL,  -- SHA-256, ver abaixo
    ADD COLUMN expansion_ordinal SMALLINT NOT NULL DEFAULT 0;

DROP INDEX IF EXISTS ux_set_idempotency;  -- versão antiga: (session_id, exercise_id, set_index, source_message_id)

CREATE UNIQUE INDEX ux_set_idempotency
    ON exercise_set (session_id, exercise_id, provenance_hash, expansion_ordinal)
    WHERE deleted_at IS NULL;
```

`provenance_hash = SHA-256` da sequência canônica `(raw_message_id, start, end)` dos spans
**resolvidos** de um `ExtractedSet` — um hash por série extraída, não por linha física. `"3x10"` é uma
`ExtractedSet` com `repeat=3`; a T12 expande em três linhas de `exercise_set` que **compartilham** o
mesmo `provenance_hash` (mesma evidência) e se distinguem por `expansion_ordinal` (0, 1, 2) — a
posição da linha dentro da expansão *daquela série*, não a posição dela na sessão. Isso é o que
`set_index` deixa de ser: um índice calculado a partir de "quantas linhas já existem na sessão" muda
de valor entre a tentativa que commitou e o retry que a sucede, porque o próprio commit alterou a
contagem — é uma chave que se invalida sozinha. `provenance_hash` e `expansion_ordinal` são funções
apenas do conteúdo já validado da extração; **não** dependem de quantas linhas existem no banco, então
são idênticos em toda tentativa da mesma extração, committada ou não.

`set_index` continua existindo, para exibição e ordenação — calculado normalmente a partir da
contagem atual, no mesmo `INSERT`, com `ON CONFLICT (session_id, exercise_id, provenance_hash,
expansion_ordinal) DO NOTHING`. O ponto central: como esse alvo do conflito **não inclui** `set_index`,
o retry cujo `INSERT` calcula um `set_index` diferente ainda colide e é descartado — o valor
"errado" de `set_index` nunca chega a ser persistido, porque a linha inteira é. `source_message_id`
sai do índice de idempotência; ele nunca sustentou séries de fragmento plural, e a T05 já expõe o
substituto correto (`raw_message_id` por span, dentro de cada `ExtractedSet`).

## Consequências

Uma série montada numa rajada preserva todas as evidências sem duplicar dado sensível em claro, sem
pedir a um LLM que aponte para texto que ele não leu, e sem que um retry parcial infle volume. Em
troca:

- A T05 expõe `raw_message_id` no contrato interno de `raw_fragments` (já previsto).
- A T08 ganha um passo determinístico pós-LLM (`align_segment_spans`) e ganha `domain/provenance.py`
  como arquivo próprio — deixa de ser algo que a T09 cria depois, porque a T08 é a primeira
  consumidora na ordem do grafo.
- A T09 e a T23 trocam "o LLM emite spans" por "o LLM cita segmentos, o código resolve spans" —
  simplifica o schema de saída do LLM (é só `list[int]`) e move toda a superfície de erro para código
  testável determinístico.
- A T12 ganha uma tabela, duas colunas em `exercise_set`, duas chaves candidatas novas (em
  `exercise_set` e em `raw_message`), um índice de idempotência substituído e uma FK que usa a
  sintaxe de coluna do `ON DELETE SET NULL` (PG15+; o `docker-compose.yml` já fixa Postgres 16).
- Os testes cobrem: os três fragmentos; offsets inválidos/fora de ordem; FK cruzada entre tenants nas
  duas direções (`exercise_set_id` e `raw_message_id`); reprocessamento que commita mas não
  checkpointa (prova que `expansion_ordinal` + `provenance_hash` bloqueiam a duplicata que
  `set_index` sozinho deixava passar); purga de `raw_message` aos 90 dias com `exercise_set_source`
  sobrevivendo com `raw_message_id IS NULL` e o snapshot intacto; span de fragmento de voz resolvido
  contra `transcript`, nunca `payload`; ausência de texto sintético.

## Condição de revisão

Reabrir se o produto passar a exigir recuperação do literal **além** dos 90 dias de retenção de
`raw_message` — hoje isso é tratado como perda aceita e intencional (é o que a política de retenção
significa). Se deixar de ser aceitável, os spans precisam de uma retenção cifrada própria,
desacoplada de `raw_message`, o que é um ADR novo, não uma extensão deste.
