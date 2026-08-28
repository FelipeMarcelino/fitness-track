# ADR-0003 — Escopo de tenant dentro da chave estrangeira

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-08-28 |
| Revisa | — (altera o schema da §5.2; não revisa decisão da §2) |

## Contexto

A §19.1 trata isolamento como duas barreiras: o repositório, que não roda sem `tenant_id`, e o RLS,
que existe porque a primeira depende de alguém lembrar. A migração 0002 implementou a segunda.

Row-level security governa **quais linhas um tenant enxerga**. Ela não governa **quais linhas um
tenant referencia**: a verificação de integridade referencial roda com row security desativada, e
isso é por construção do Postgres — caso contrário uma linha invisível poderia ser apagada por baixo
de uma chave estrangeira que a aponta.

O schema da §5.2 dá a `exercise_set` duas referências a `exercise`:

```sql
FOREIGN KEY (exercise_id) REFERENCES exercise(id)                                    -- sem tenant
FOREIGN KEY (exercise_id, exercise_tenant_id) REFERENCES exercise(id, tenant_id)     -- com tenant
```

A segunda é `MATCH SIMPLE`, que é o padrão: uma chave composta contendo qualquer `NULL` é
**ignorada por inteiro**. E `ck_set_exercise_scope` permite `exercise_tenant_id IS NULL`, cujo
significado pretendido é "exercício do catálogo global". Nada verificava que o exercício apontado
de fato era global, e a primeira chave aceitava qualquer id da tabela.

Resultado, reproduzido como `fittrack_runtime` ligado ao tenant A antes da correção: A grava uma
série contra o exercício privado de B. Duas consequências:

- **Oráculo de existência.** Violação de FK significa "esse id não existe"; sucesso significa "existe
  e é de alguém". A é capaz de enumerar os ids privados de B sem nunca ler uma linha de B.
- **Negação de serviço cruzada.** A referência `NO ACTION` impede B de apagar o próprio exercício,
  sem nada do lado de B que explique o motivo — a linha que bloqueia é invisível para ele.

`plan_item` tem a mesma forma. `program_milestone` era pior: `exercise_id` sem nenhuma coluna de
escopo ao lado.

O teste `test_every_table_with_a_tenant_column_has_a_policy` não vê nada disso. Toda tabela envolvida
tem coluna de tenant e tem política; o vazamento não está em quem lê, está em quem aponta.

## Decisão

Migração 0003. Tornar a parte de tenant da chave **impossível de pular**, trocando o `NULL` que
desliga o `MATCH SIMPLE` por um valor que nenhum tenant pode ter:

```sql
ALTER TABLE exercise
  ADD COLUMN tenant_scope bigint GENERATED ALWAYS AS (coalesce(tenant_id, 0)) STORED;
ALTER TABLE exercise ADD CONSTRAINT uq_exercise_id_scope UNIQUE (id, tenant_scope);
```

Cada tabela que referencia `exercise` ganha o `exercise_scope` correspondente, também gerado, e uma
única chave estrangeira composta `(exercise_id, exercise_scope) → exercise (id, tenant_scope)`. As
chaves sem escopo são removidas. `program_milestone` ganha o `exercise_tenant_id` e o `CHECK` que as
outras duas já tinham.

`0` funciona como sentinela porque `tenant_id` vem de uma sequência que começa em 1. Colunas geradas
porque o escopo não é um dado que alguém informa — é uma função da linha, e deixá-lo editável seria
recriar o problema com outro nome.

Isto **altera o schema da §5.2**, que é a razão deste ADR existir: quem ler a spec vai encontrar as
duas chaves antigas em `exercise_set`, `plan_item` e `program_milestone`.

## Consequências

**Melhora.** O vazamento fecha nos dois caminhos, e os dois são testados: alegar que o exercício é
global dá violação de chave estrangeira, e nomear o tenant do outro honestamente dá violação de
`CHECK`. Os dois casos legítimos — referenciar o catálogo global e referenciar o próprio exercício
privado — continuam funcionando, e também são testados, porque uma chave que rejeitasse o catálogo
seria um bug pior que o corrigido e não apareceria num teste que só verifica que o vazamento fechou.

**Piora.** Três colunas geradas a mais, e um índice único a mais em `exercise`. Adicionar coluna
gerada `STORED` reescreve a tabela: irrelevante hoje, não em uma base grande.

**Passa a exigir atenção.** Toda escrita nessas três tabelas precisa preencher `exercise_tenant_id`
corretamente — `NULL` para exercício do catálogo, o próprio tenant para exercício privado. Errar
agora falha alto, na chave, em vez de gravar silenciosamente uma referência cruzada. Qualquer tabela
nova que referencie `exercise` deve usar o par com escopo, nunca `exercise_id` sozinho.

## Condição de revisão

Se a §5.2 passar a modelar o catálogo global como um tenant real (um `tenant_id` reservado em vez de
`NULL`), o `coalesce` deixa de ser necessário: a chave composta passa a nunca conter `NULL` sozinha,
e as colunas geradas podem sair em favor de uma referência direta a `(id, tenant_id)`. Vale reabrir
quando essa mudança for considerada por outro motivo — não vale forçá-la só por isto.
