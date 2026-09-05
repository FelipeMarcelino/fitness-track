# ADR-0011 — SDKs nativos na camada de provider, `BaseMessage` só como contrato de entrada

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-05 |
| Revisa | §7.4 da spec (linha "SDK LangChain") |

## Contexto

A §7.4 descreve a camada de provider em termos de `langchain_groq.ChatGroq` e
`langchain_anthropic.ChatAnthropic`, e o item 5 da mesma seção cita `with_structured_output` do
LangChain como o normalizador de structured output.

O `pyproject.toml` instala outra coisa. Estão declarados os SDKs **nativos** — `groq`, `anthropic`,
`openai` — mais `langchain-core`, que entra por causa do LangGraph. Os pacotes `langchain-groq` e
`langchain-anthropic` **não são dependências do projeto**, e nenhum código os importa.

A §7.4 se contradiz sozinha, e é o resto da própria seção que a contradiz. A tabela de diferenças
existe para enumerar o que o gateway precisa absorver **à mão**, e as consequências práticas que ela
lista são todas assimetrias finas:

- `temperature` é aceito no Groq e **rejeitado** nos modelos Anthropic de nova geração;
- `reasoning_format` é inválido no `gpt-oss` e válido em *outros* modelos do mesmo provider — por
  isso o item 1 diz, com todas as letras, que o mapa de parâmetros permitidos é por
  **`(provider, modelo)`**, não por provider;
- tool calling e structured output **não coexistem** no Groq e coexistem na Anthropic;
- prefill de assistant é 400 nos modelos Anthropic atuais.

Um wrapper de chat genérico é exatamente a camada que apaga essa granularidade: ele expõe uma
superfície comum por provider, não por `(provider, modelo)`, que é a chave que a §7.4 exige.

## Decisão

A camada de provider é escrita sobre os **SDKs nativos**. `llm/providers/base.py` declara a porta
`LLMProvider`; `groq.py` e `anthropic.py` a implementam falando o SDK de cada um. Nenhum objeto de
SDK atravessa a fronteira de `llm/providers/`.

`BaseMessage` do `langchain-core` **permanece** como o contrato de entrada do `ainvoke`, exatamente
como a §7.1 o tipa. Não é concessão ao LangChain: o `messages` do `GraphState` da §8.2 usa o reducer
`add_messages`, que fala `BaseMessage`, e os nós do grafo já carregam esse vocabulário. Converter
para dicionário na fronteira do gateway e de volta no nó seria duas traduções para não usar um tipo
que já está instalado.

O mapa de parâmetros permitidos vive em código, chaveado por `(provider, modelo)`, e é aplicado
antes de cada chamada.

## Alternativas recusadas

1. **Acrescentar `langchain-groq` e `langchain-anthropic`.** Duas dependências novas — cada uma com
   sua própria janela de compatibilidade contra o `langchain-core` já fixado — para esconder
   justamente as regras por `(provider, modelo)` que a §7.4 manda tornar explícitas. Dependência
   nova exige justificativa, e a justificativa aqui seria "para não escrever o mapa de parâmetros",
   que é o trabalho da tarefa.
2. **Abandonar `BaseMessage` e passar dicionários.** O estado do grafo fala `BaseMessage` por causa
   do `add_messages`; trocar o contrato de entrada obrigaria a converter nas duas pontas.
3. **Usar `with_structured_output` como fonte da verdade do schema.** A convenção do `AGENTS.md`
   ("toda saída de LLM é validada com Pydantic; a validação é a fonte da verdade, não a promessa de
   structured output do provider") e o item 5 da própria §7.4 já dizem que a validação Pydantic
   acontece de qualquer forma. Se ela é obrigatória nos dois caminhos, a normalização do LangChain
   deixa de ser carga estrutural e vira conveniência — que não paga duas dependências.

## Consequências

- A linha "SDK LangChain" da tabela da §7.4 vira **errata**: o gateway implementa a porta
  `LLMProvider` sobre SDK nativo. As outras linhas da tabela seguem válidas — elas descrevem o
  comportamento dos providers, não o do wrapper.
- O mapa de parâmetros por `(provider, modelo)` é código testável, e `tests/unit/test_llm_provider_params.py`
  reprova `temperature` no caminho Anthropic e `reasoning_format` no caminho `gpt-oss`. Uma regra
  que hoje é uma linha de tabela numa spec passa a ser um teste.
- `langchain-core` continua dependência — pelo LangGraph e pelo `BaseMessage`. Nenhum
  `langchain-<provider>` entra.
- O custo é real: todo recurso de provider que o LangChain normalizaria — nomes de parâmetro,
  formato de tool call, blocos de conteúdo — é escrito e testado aqui. Em troca, a assimetria fica
  visível no lugar onde ela precisa ser decidida.
- A invariante 4 não muda de mecanismo: nome de modelo continua só em `config/models.yaml`,
  resolvido por `ModelsConfig.resolve(agent, role)`. O provider recebe o identificador já resolvido
  e nunca o escolhe.

## Condição de revisão

Reabrir se um terceiro e um quarto provider entrarem e os adapters começarem a repetir uns aos
outros — aí a normalização vira código comum nosso, não necessariamente LangChain. Reabrir também
se o `langchain-core` passar a oferecer, sem os pacotes por provider, algo que o caminho nativo não
alcança (cache unificado de prompt, por exemplo): nesse caso a dependência já paga está fazendo o
trabalho, e a decisão muda sem custo novo.
