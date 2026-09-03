# ADR-0007 — STT fora do `LLMGateway`, configurado em `models.yaml`

| Campo | Valor |
| --- | --- |
| Estado | aceito |
| Data | 2026-09-02 |
| Revisa | — (interpreta a invariante 4 do `CLAUDE.md` para a §11.1) |

## Contexto

A invariante 4 diz que nome de modelo não aparece em código: os nomes vivem em
`config/models.yaml` e são resolvidos **por papel (`LLMRole`) dentro do `LLMGateway`**. A §11.1
exige um modelo de transcrição (`whisper`), que é um nome de modelo como qualquer outro — e a
S02-T07 precisa dele antes de o `LLMGateway` existir (ele entra na Sprint 03).

Duas leituras eram possíveis, e nenhuma serve inteira:

1. **Criar um papel `TRANSCRIBER` em `roles:`.** A tabela da §7.2 é fechada em dez papéis, e
   `tests/unit/test_judge_config.py` verifica exatamente esse conjunto. Pior que o teste: um papel
   é uma *classe de custo e capacidade* com primário, fallback de outro provider,
   `reasoning_effort` e `timeout_s`, e é avaliado pelo golden set da §21.1. Transcrição não tem
   prompt de sistema, não tem tier de raciocínio, não tem fallback entre providers e não é
   avaliável pelo golden set. Seria uma linha na tabela de tiering que o gateway não saberia
   resolver e a suíte de avaliação não saberia medir.
2. **Instanciar o cliente do provider no serviço.** Viola a invariante 4 diretamente.

## Decisão

`config/models.yaml` ganha uma seção `stt:` de primeiro nível — irmã de `roles:` e `agents:`, não
membro de `roles:`. Ela é tipada por `SttConfig` em `config.py`, validada no boot como as outras, e
carrega o provider, o modelo, o idioma, o `response_format`, o `timeout_s` e as três regras
numéricas da §11.3.

O serviço de transcrição recebe essa configuração já validada e não conhece nome de modelo nenhum.
A interface abstrata é o `AudioTranscriber` que a nota de arquitetura da §11.3 pede; a
implementação `GroqTranscriber` fala o endpoint da §11.1 e valida a resposta com Pydantic.

`configured_providers` passa a contar o provider de STT, então um deployment que nomeia a Groq sem
`GROQ_API_KEY` é reportado no boot em vez de falhar na primeira mensagem de voz.

## Consequências

- A invariante 4 vale para o modelo de STT com o mesmo mecanismo dos demais: um único arquivo
  versionado, recarregável, e `tests/unit/test_stt.py` reprova qualquer identificador de modelo de
  transcrição que apareça em Python.
- A tabela de dez papéis da §7.2 continua intacta, e o `LLMGateway` da Sprint 03 não precisa
  aprender um caso que não é chat.
- O custo: `stt` fica fora do relatório de overrides do CI e fora do golden set. Regressão de
  qualidade de transcrição é medida pelo eval da §21, não pelo golden set por papel.
- `ModelsConfig.stt` é opcional no tipo e obrigatório no arquivo comitado. Um `models.yaml` parcial
  — a forma que os testes de loader escrevem — continua falhando pelo motivo que está sendo
  testado, e `require_stt()` transforma a ausência em erro no ponto de uso.
- Migrar para `faster-whisper` self-hosted, como a §11.3 prevê, é uma implementação nova de
  `AudioTranscriber` e uma linha trocada nessa seção.

## Condição de revisão

Reabrir quando o `LLMGateway` da Sprint 03 estiver de pé e houver um segundo modelo não-chat
(embeddings já está em `rag.yaml`, o que é o mesmo padrão). Se três seções distintas passarem a
descrever "modelo que não é papel", vale unificá-las sob um mecanismo comum em vez de uma seção por
uso.
