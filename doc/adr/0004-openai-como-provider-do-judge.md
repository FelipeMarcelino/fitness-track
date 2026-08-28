# ADR-0004 — OpenAI como provider do LLM-as-judge

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-08-28 |
| Revisa | AD-19 da §2, somente para o papel `JUDGE`; substitui ADR-0002 |

## Contexto

O `JUDGE` foi implementado com Anthropic e `claude-opus-5`, separado do primário Groq que produz as
respostas avaliadas. A conta Anthropic disponível ao projeto não tem saldo e retorna erro de
crédito antes de qualquer caso ser pontuado. O ADR-0002 manteve o job visível, mas suspendeu seu
poder de veto com `continue-on-error: true`.

A conta OpenAI do projeto tem credencial própria. A OpenAI Responses API oferece saída estruturada
por JSON Schema, compatível com o contrato atual: o provider orienta o formato e o Pydantic continua
sendo a validação final. O papel permanece independente do provider primário Groq e do modelo
`gpt-oss-120b` usado pelos agentes.

## Decisão

O LLM-as-judge passa a usar OpenAI com estas condições:

- modelo `gpt-5.6-terra`, `reasoning_effort: high`, definido somente em `config/models.yaml`;
- Responses API com Structured Outputs em modo estrito e validação posterior por Pydantic;
- `OPENAI_API_KEY` como credencial local e secret do GitHub Actions;
- modo estrito do runner identificado por `--backend openai`;
- `config/models.yaml` incluído nos paths que disparam a rodada do judge;
- remoção de `continue-on-error`, reativando o veto de segurança e fidelidade numérica.

Os fallbacks dos papéis de produto continuam na Anthropic. Esta decisão muda somente o provider
offline do `JUDGE` e não antecipa a implementação do `LLMGateway`.

## Consequências

- O CI volta a reprovar quando o judge não executa ou quando uma rubrica bloqueante falha em uma
  rodada calibrada.
- A calibração existente de 20 casos continua protegendo a troca de modelo: mais de dois desacordos
  com os rótulos humanos descartam a rodada sem reprovar o produto.
- A amostra completa continua fazendo 60 chamadas por execução aplicável; o modelo escolhido
  equilibra capacidade e custo, mas precisa ser acompanhado por uso e latência reais.
- Uma credencial válida não garante crédito permanente. Erros de saldo, limite de organização ou
  limite de projeto continuam falhando o job e exigem ação no faturamento, não retry automático.
- A Anthropic permanece nas dependências e nos secrets enquanto for fallback dos papéis de produto.

## Condição de revisão

Reavaliar esta decisão se o judge não atingir 18 dos 20 casos de calibração, se custo ou latência
impedirem o uso em toda PR aplicável, se o acesso ao modelo deixar de existir, ou se o provider
passar a compartilhar o mesmo modo de falha do modelo avaliado. Uma nova troca exige ADR, rodada de
calibração completa e CI verde antes de alterar o portão.
