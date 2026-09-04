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

## Decisão

O LLM só extrai o literal e a unidade. Para toda quantidade que exige normalização, a saída crua usa:

```python
class QuantityLiteral(BaseModel):
    value: Decimal
    unit: Literal["kg", "lb", "m", "km", "cm", "h", "scale_0_10"]
    unit_origin: Literal["explicit", "defaulted"]
```

`load_kg`, `distance_m` e valores equivalentes aparecem apenas no DTO produzido por
`domain/units.py`. Entrada de musculação sem unidade recebe `unit="kg"` e
`unit_origin="defaulted"`; entrada explícita preserva a unidade canônica (`lbs`/`libras` → `lb`) e o
número sem conversão no modelo.

O conversor usa somente `Decimal`: lb → kg multiplica por `Decimal("0.45359237")`; km → m multiplica
por `Decimal("1000")`. Toda normalização para as colunas `NUMERIC(...,2)` aplica
`quantize(Decimal("0.01"), ROUND_HALF_UP)` exatamente uma vez, no limite de persistência. A mesma
fronteira calcula RIR derivado a partir de RPE; RIR explícito continua intocado. O gateway/prompt não
tem permissão para emitir campo convertido, arredondado ou derivado.

## Consequências

O schema de extração fica mais explícito e testes podem fixar entradas como `100 lb → 45.36 kg` sem
simular raciocínio do modelo. Há conversor e testes a mais, mas a mesma regra cobre retry, fallback e
qualquer provider. A errata da §9.5 precisa substituir a instrução de conversão no prompt pelo
contrato literal e pela fronteira determinística.

## Condição de revisão

Reabrir somente se surgir uma unidade cuja conversão não possa ser expressa por constantes e regras de
arredondamento versionadas; a unidade não entra no prompt antes de a regra determinística ser aceita.
