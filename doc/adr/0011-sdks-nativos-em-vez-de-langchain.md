# ADR-0011 — SDKs nativos em vez de LangChain

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-04 |
| Revisa | §7.4 da spec |

## Contexto

A §7.4 citava `ChatGroq` e `ChatAnthropic`, mas as dependências instaladas são os
SDKs nativos `groq` e `anthropic`; `langchain-groq` e `langchain-anthropic` não fazem
parte do projeto. Adicionar adaptadores LangChain só para esconder diferenças de wire
format criaria uma segunda camada de comportamento sem requisito de produto.

O gateway ainda precisa aceitar mensagens no contrato já usado pelo grafo. Isso não
exige que a chamada remota também seja feita por LangChain.

## Decisão

`LLMGateway` usa adapters injetáveis sobre os SDKs nativos de cada provider. Ele pode
aceitar `BaseMessage` de `langchain-core` como contrato de entrada, mas converte para o
formato nativo na fronteira do adapter. A normalização de parâmetros permanece por
`(provider, model)`, e toda resposta passa por `schema.model_validate()` depois da
resposta do provider.

Nenhum agente instancia SDK de provider diretamente. Nomes de modelo continuam
exclusivos de `config/models.yaml`, resolvidos por `ModelsConfig.resolve(agent, role)`.

## Consequências

O gateway torna explícitas as diferenças de structured output, tools, cache e
raciocínio de cada API, com fakes simples em testes e sem nova dependência. Em troca,
os adapters mantêm código de conversão que uma integração LangChain teria ocultado.
Esse código fica concentrado no gateway, onde pode ser testado por provider e modelo.

Adicionar um pacote `langchain-*` no futuro exige justificar a capacidade que os
adapters nativos não cobrem e provar que ele não viola a política de parâmetros ou a
validação Pydantic.

## Condição de revisão

Reabrir se um provider suportado deixar de expor no SDK nativo uma capacidade necessária
ao produto, ou se uma integração LangChain oferecer essa capacidade com contrato estável
e redução demonstrável de manutenção.
