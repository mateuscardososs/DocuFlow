# Avaliação da qualidade da extração

Este guia descreve como executar uma avaliação manual do pipeline real de leitura de PDF/OCR, extração estruturada e validação comercial com os documentos fictícios de [`samples/documents/`](../samples/documents/). A avaliação ajuda a analisar esta amostra e configuração específicas; não é uma medida geral de desempenho.

O avaliador chama `DocumentProcessingService` diretamente. Não acessa banco, Redis, API, produção ou ERP; não aprova documentos nem altera pedidos. Ele carrega `samples/ground_truth/manifest.json` para comparar os resultados localmente, mas nunca envia o gabarito ao provedor. A extração recebe apenas o texto das páginas lido do arquivo. O avaliador aceita somente Ollama local em loopback (ou o alias local `host.docker.internal`, convertido para loopback quando executado no host). Não baixa nem troca modelos.

## Pré-requisitos

1. Prepare o ambiente Python do projeto. Para casos que usam OCR, instale o Tesseract e os idiomas configurados, normalmente `por+eng`; veja o guia de [desenvolvimento local](desenvolvimento-local.md).
2. Inicie o Ollama local e instale manualmente o modelo que será usado. O avaliador consulta `/api/tags` antes de começar e encerra sem avaliar se o serviço não responder ou o modelo não estiver instalado.
3. Configure `DOCUFLOW_OLLAMA_BASE_URL` e `DOCUFLOW_OLLAMA_MODEL` no ambiente do processo ou no `.env` privado. O script lê do `.env` apenas opções permitidas do provedor e do OCR; ignora credenciais de banco e autenticação e não exibe valores de configuração.

As opções disponíveis de extração/OCR são `DOCUFLOW_OLLAMA_TIMEOUT_SECONDS`, `DOCUFLOW_OCR_LANGUAGE`, `DOCUFLOW_OCR_DPI`, `DOCUFLOW_OCR_TIMEOUT_SECONDS`, `DOCUFLOW_NATIVE_TEXT_MIN_CHARS` e `DOCUFLOW_TESSERACT_CMD`. O endpoint Ollama deve permanecer local; não configure endpoint de produção, hospedado ou pago.

## Executar

Confirme explicitamente a execução pelo pipeline real:

```bash
.venv/bin/python scripts/evaluate_extraction.py --run
```

Por padrão, o avaliador processa os nove documentos distintos, casos 01–09. O caso 10 é uma duplicata byte a byte do caso 01 e permanece um cenário funcional separado; ele não é enviado ao modelo uma segunda vez.

Para limitar os casos e alterar o timeout somente naquela execução:

```bash
.venv/bin/python scripts/evaluate_extraction.py --run --case-ids 01,06,07,08,09 --timeout-seconds 180
```

- `--case-ids` recebe IDs separados por vírgulas. IDs desconhecidos ou entradas vazias são recusados antes da chamada ao modelo.
- `--timeout-seconds` define um timeout positivo por tentativa nesta execução. Sem a opção, mantém o valor de `DOCUFLOW_OLLAMA_TIMEOUT_SECONDS`, cujo padrão é 60 segundos.
- O limite padrão permanece em duas tentativas por caso.
- Sem uma seleção, o avaliador usa os nove casos distintos. Uma tentativa usa os documentos associados às amostras e não altera o manifesto.

Cada execução precisa de um diretório de saída novo e grava `results.json`, `results.csv` e `summary.md` em `var/evaluations/<data-UTC>/`. `var/` é ignorado pelo Git; os artefatos brutos contêm texto extraído e campos sintéticos detalhados. Uma execução com Ollama indisponível ou modelo ausente é registrada como `not_executed`, sem métricas e sem download automático.

O caso duplicado tem um teste funcional separado na API:

```bash
.venv/bin/python -m unittest backend.tests.test_document_persistence.DocumentPersistenceApiTests.test_identical_upload_is_reused_inside_workspace -v
```

## Métricas e interpretação

- **Acerto por campo:** compara valores esperados e extraídos após normalização explícita. Texto usa normalização Unicode NFKC, colapso de espaços e comparação sem distinção de caixa; quantidade e dinheiro são comparados como `Decimal`.
- **Campos ausentes:** ficam fora do denominador de campos presentes. O reconhecimento de ausência é medido à parte; uma ausência prevista não é contada como erro de extração de campo presente.
- **Evidências:** para campos esperados presentes, verifica se o trecho citado aparece no texto da página indicada após normalização de espaços. Isso não comprova que o modelo interpretou corretamente o trecho ou o associou semanticamente ao campo.
- **Resultado comercial:** compara separadamente status e códigos de achados com as expectativas calculadas pela validação determinística e pela política registrada no manifesto. Um documento comercialmente divergente pode ter sido extraído corretamente.
- **Modalidade e duração:** os relatórios separam PDF nativo de OCR (PDF digitalizado e imagem), registram a duração de cada caso e distinguem falhas de processamento de resultados pontuados.

Uma amostra pequena e sintética descreve somente os arquivos, modelo, parâmetros e ambiente daquela execução. Não generalize taxas observadas para outros documentos ou configurações. Os resultados detalhados pertencem aos diretórios ignorados em `var/evaluations/`, não a este guia.

Os testes unitários das métricas usam respostas simuladas e não chamam Ollama ou Tesseract:

```bash
.venv/bin/python -m unittest backend.tests.test_extraction_evaluation -v
```
