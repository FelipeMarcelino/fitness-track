# ADR-0010 — Fronteira de manutenção cross-tenant

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-04 |
| Revisa | — (complementa §§5.3, 8.4 e 19.1 da spec) |

## Contexto

O runtime usa RLS e cada repositório exige `tenant_id`. Essa é a fronteira correta
para trabalho de produto, mas quatro operações legítimas não começam com um tenant:
claim global de `outbound_queue`, manutenção de sessões, resolução pré-tenant de
identidade e as tabelas do LangGraph. Usar o DSN do owner, dar `BYPASSRLS` ao runtime
ou torná-lo membro de uma role privilegiada faria as policies existirem sem serem
avaliadas.

As tabelas do LangGraph exigem uma segunda abordagem. O pool `psycopg` do saver não
compartilha o `SET LOCAL app.tenant_id` da sessão SQLAlchemy, e o `AsyncPostgresStore`
não tem uma coluna de tenant. Uma policy baseada só em `thread_id` bloquearia acessos
legítimos ou deixaria o Store fora do isolamento.

## Decisão

Para varreduras globais de tabelas de domínio sob `FORCE ROW LEVEL SECURITY`, a única
porta é uma função `SECURITY DEFINER` estreita, com `search_path` fixo, parâmetros
tipados e retorno mínimo. Seu dono é uma role `NOLOGIN NOSUPERUSER BYPASSRLS`, sem
membros e com apenas os grants de tabela, coluna e sequência necessários. O runtime
recebe somente `EXECUTE`; ele nunca recebe a role dona nem o DSN do owner.

Para a persistência do LangGraph:

- `thread_id` é construído exclusivamente pelo código como `f"tenant:{tenant_id}"`;
- `fittrack_graph` é um principal `LOGIN NOSUPERUSER NOBYPASSRLS`, fora de
  `fittrack_app`, com DML somente em `checkpoints`, `checkpoint_blobs`,
  `checkpoint_writes` e `store`, e sem acesso a tabelas de domínio;
- o bootstrap executa Alembic, depois `saver.setup()`/`store.setup()` como owner,
  revoga grants herdados e só então concede os grants mínimos a `fittrack_graph`;
- `TenantEncryptedSaver` envolve inclusive valores primitivos, cifra-os com AAD ligado
  ao tenant/thread e não expõe conteúdo em `checkpoint` ou `metadata`;
- `TenantScopedStore` prefixa toda operação com `("tenant", str(tenant_id),
  "profile")`, cifra o valor com AAD de tenant, namespace e key, e é a única porta
  entregue ao grafo. O Store bruto nunca chega a um nó nem ao runtime.

Os jobs de manutenção da Sprint 04 reutilizam a mesma função estreita. A fronteira
pré-tenant já existente para identidade continua limitada às suas duas funções; ela
não é uma autorização para consultas globais novas.

## Consequências

O runtime permanece incapaz de ler dados de outro tenant mesmo se uma query esquecer
o filtro, enquanto tarefas operacionais ganham uma superfície pequena, auditável e
testável. O custo é manter roles, grants pós-`setup()` e testes de privilégio
explícitos. As tabelas do LangGraph não usam RLS: isolamento lógico, menor privilégio
e cifra contra dump são camadas separadas e todas obrigatórias.

Um teste que só funciona conectado como superuser é inválido como prova de isolamento.
Da mesma forma, uma alteração de schema da biblioteca deve falhar alto se a lista de
relações esperadas no bootstrap divergir da lista criada por `setup()`.

## Condição de revisão

Reabrir se o LangGraph passar a suportar escopo de tenant verificável pelo próprio
backend sem pool separado, ou se uma operação global precisar de uma superfície que
uma função `SECURITY DEFINER` mínima não consegue expressar. A alternativa deve provar
que não amplia os privilégios do runtime.
