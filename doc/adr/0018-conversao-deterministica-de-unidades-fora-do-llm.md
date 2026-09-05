# ADR-0018 — Conversão determinística de unidades fora do LLM

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-04 |
| Revisa | — (corrige a regra 1 da §9.5 da spec) |

## Contexto

A §9.5 pedia que o prompt convertesse libras em quilogramas e quilômetros em metros, enquanto o
contrato expunha campos normalizados como `load_kg`. Isso delega multiplicação e arredondamento ao
LLM, contrariando a invariante 1: números persistidos e usados em análise não podem depender de
aritmética probabilística.

O mesmo problema existe para tempo. `exercise_set` persiste `duration_s`, `hold_s` e `rest_s` — os
três em **segundos** — mas o exemplo canônico de entrada da §9.7 é `"Corri 40 minutos"`. Sem um
literal de tempo, o schema de extração ficaria com a mesma escolha ruim que a §9.5 tinha para
distância: ou o prompt devolve `duration_s=2400` (o LLM multiplicou por 60 — a mesma aritmética
proibida) ou o campo vem em minutos e alguém converte informalmente antes da persistência.

## Decisão

O LLM só extrai o literal e a unidade. Para toda quantidade que exige normalização, a saída crua usa:

```python
class QuantityLiteral(BaseModel):
    value: Decimal
    unit: Literal["kg", "lb", "m", "km", "cm", "h", "s", "min", "scale_0_10"]
    unit_origin: Literal["explicit", "defaulted"]
```

`load_kg`, `distance_m` e valores equivalentes aparecem apenas no DTO produzido por
`domain/units.py`. Entrada de musculação sem unidade recebe `unit="kg"` e
`unit_origin="defaulted"`; entrada explícita preserva a unidade canônica (`lbs`/`libras` → `lb`) e o
número sem conversão no modelo. `"Corri 40 minutos"` vira `QuantityLiteral(value=Decimal("40"),
unit="min", unit_origin="explicit")` — o LLM nunca escreve `2400`; `duration_s`/`hold_s`/`rest_s` só
existem depois de `domain/units.py` multiplicar. Segundo explícito (`"descansei 90 segundos"`) usa
`unit="s"` e passa direto, sem conversão.

O conversor usa somente `Decimal`: lb → kg multiplica por `Decimal("0.45359237")`; km → m multiplica
por `Decimal("1000")`; cm → m multiplica por `Decimal("0.01")`; min → s multiplica por `Decimal("60")`; h → s multiplica por
`Decimal("3600")`. Toda normalização para as colunas
`NUMERIC(...,2)` (cargas e distâncias) aplica `quantize(Decimal("0.01"), ROUND_HALF_UP)` exatamente
uma vez, no limite de persistência; `duration_s`/`hold_s`/`rest_s` são `INTEGER` (§5.2), então o
mesmo limite aplica `to_integral_value(ROUND_HALF_UP)` em vez de `quantize` para duas casas — a regra
de "uma vez, no limite" continua a mesma, só a função de arredondamento muda com o tipo de destino. O
gateway/prompt não tem permissão para emitir campo convertido, arredondado ou derivado.

**Fora de escopo, de propósito: RIR.** Este ADR cobre só a conversão de unidade de uma
`QuantityLiteral` (kg/lb, km/m e as demais da §9.5) — grandeza física para grandeza física, com
constante de conversão fixa. `RIR ≈ 10 − RPE` não é conversão de unidade: é uma inferência derivada
de outro campo do próprio domínio de treino, e mora em `domain/rpe.py`, não em `domain/units.py`
(T09, §9.6). As duas fronteiras são determinísticas e as duas rodam no limite de persistência, mas
são módulos diferentes porque resolvem problemas diferentes — juntar os dois em `units.py` faria o
módulo de unidades também precisar conhecer a tabela de RPE/RIR da §9.6, que não tem nada a ver com
quilogramas. RIR explícito nunca é sobrescrito pelo derivado (§9.6, T09).

## Consequências

O schema de extração fica mais explícito e testes podem fixar entradas como `100 lb → 45.36 kg`,
`50 cm → 0.50 m`, `40 min → 2400 s` e `1 h → 3600 s` sem simular raciocínio do modelo. Há conversor e testes a mais,
mas a mesma regra
cobre retry, fallback e qualquer provider. A errata da §9.5 precisa substituir a instrução de
conversão no prompt pelo contrato literal e pela fronteira determinística, para distância **e** para
tempo. Fixar RIR em `domain/rpe.py` (e não aqui) evita que uma leitura futura deste ADR reintroduza a
derivação em `units.py` e duplique o cálculo.

## Condição de revisão

Reabrir somente se surgir uma unidade cuja conversão não possa ser expressa por constantes e regras de
arredondamento versionadas; a unidade não entra no prompt antes de a regra determinística ser aceita.
