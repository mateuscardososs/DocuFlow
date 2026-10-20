# Amostras comerciais fictícias do DocuFlow

Este conjunto contém dez documentos sintéticos para demonstrações e avaliação da extração. Use somente os arquivos em `samples/documents/` como entradas de upload. O manifesto em `samples/ground_truth/manifest.json` é gabarito de avaliação: não o envie ao provedor de IA.

## Pedido associado

Todos os casos devem ser associados, inclusive os divergentes, ao pedido de referência atual:

| Campo | Valor |
|---|---|
| Referência | `PC-2026-0001` |
| Fornecedor | `Metalúrgica Horizonte Ltda.` |
| Identificador fictício | `FORN-0001` |
| Moeda | `BRL` |
| `ARR-001` — Arruela zincada M8 | Quantidade `10`; preço `85.50`; total `855.00` |
| `PAR-002` — Parafuso sextavado M8 | Quantidade `5`; preço `79.00`; total `395.00` |
| Total do pedido | `1250.00` |

O pedido é definido uma única vez em `backend/app/reference_order_data.py`; o seed e o gerador usam esse mesmo contrato. O caso de fornecedor divergente não cadastra outro fornecedor.

## Casos

| Caso | Arquivo | Tipo | Conteúdo e expectativa |
|---|---|---|---|
| 01 | `01_correto.pdf` | `native_pdf` | Conteúdo base e número `DOC-DEMO-001`; nenhuma divergência; `ready_for_review`. |
| 02 | `02_preco_divergente.pdf` | `native_pdf` | Primeiro item `10 × 90.00 = 900.00`; segundo item `395.00`; total `1295.00`. Cálculos consistentes, preço e total diferentes do pedido; `divergent`. |
| 03 | `03_total_incorreto.pdf` | `native_pdf` | Itens base e total impresso `1300.00`. O gabarito preserva esse total; soma e total do pedido divergem; `divergent`. |
| 04 | `04_quantidade_divergente.pdf` | `native_pdf` | Primeiro item `12 × 85.50 = 1026.00`; segundo item `395.00`; total `1421.00`. Quantidade e total diferem do pedido; `divergent`. |
| 05 | `05_fornecedor_divergente.pdf` | `native_pdf` | Fornecedor `Comercial Aurora Fictícia Ltda.` e identificador `FORN-0099`; demais valores iguais ao base; `divergent`. |
| 06 | `06_numero_ausente.pdf` | `native_pdf` | Número omitido por completo. Não inferir pelo nome do arquivo; as quatro partes do campo ficam `null`; `blocked`. |
| 07 | `07_data_ausente.pdf` | `native_pdf` | Data omitida por completo. Não completar pela data atual nem por metadados; `blocked`. |
| 08 | `08_correto_digitalizado.pdf` | `scanned_pdf` | Conteúdo comercial base, número `DOC-DEMO-008`, uma página em tons de cinza e sem camada de texto, rasterizada a aproximadamente 200 DPI; `ready_for_review` se reconhecido fielmente. |
| 09 | `09_correto_foto.png` | `image` | Conteúdo base, número `DOC-DEMO-009`, imagem PNG em tons de cinza a 200 DPI com rotação pequena e fixa; `ready_for_review` se reconhecido fielmente. |
| 10 | `10_duplicata_exata.pdf` | `native_pdf` | Cópia byte a byte de `01_correto.pdf`, inclusive o número `DOC-DEMO-001`; o mesmo workspace deve detectar a duplicidade pelo SHA-256. |

Os status finais e códigos de achado do manifesto são calculados por `validate_document` com a política padrão atual do projeto. Não são regras paralelas do gerador. A política registrada é versão `0`, tolerância padrão de totais `0.01` e nenhuma sobrescrita por moeda. Divergências nunca são corrigidas para coincidir com o pedido.

## Manifesto e evidências

Cada entrada do manifesto registra o `case_id`, nome, tipo, SHA-256, pedido associado, documento verdadeiro extraído esperado, texto visível esperado por página, valores originais/normalizados, evidências, campos ausentes e cálculo decimal de cada item. Em uma seção separada, registra o `final_status`, códigos e severidades esperados pela validação comercial e o comportamento esperado de duplicidade no mesmo ou em outro workspace.

O caso 01 e o 10 usam exatamente os mesmos bytes. Os outros casos têm conteúdo/metadados próprios. A evidência de OCR no manifesto representa o trecho impresso esperado, não o resultado de uma execução do Tesseract. Para comparar texto OCR com evidência, o manifesto documenta a mesma normalização de espaço usada pelo helper atual: colapsar whitespace consecutivo em um espaço e remover espaços das bordas (`normalize_spaces`). Isso não muda nem relaxa a validação de produção.

Todos os documentos exibem `DOCUMENTO FICTÍCIO PARA TESTES — SEM VALIDADE FISCAL`. Os exemplos não incluem CNPJ, endereço, telefone, conta bancária ou logotipo real. O gabarito não é incluído nos PDFs ou na imagem.

## Dependências e geração

O gerador é local e não acessa banco, Redis, OCR ou IA. O ambiente Python do projeto já inclui Pillow e pypdfium2; ReportLab fica em `requirements-samples.txt`, separado das dependências instaladas nas imagens de produção. ReportLab 5.0.1 declara compatibilidade com Python 3.14 no PyPI; a documentação de instalação do projeto informa que ReportLab 4 ou superior é distribuído em Python puro ([compatibilidade no PyPI](https://pypi.org/project/reportlab/), [instalação oficial](https://docs.reportlab.com/install/open_source_installation/)).

Após criar `.venv` e instalar as dependências normais de desenvolvimento, instale a dependência opcional e gere os arquivos:

```bash
.venv/bin/python -m pip install -r requirements-samples.txt
.venv/bin/python scripts/generate_demo_documents.py
```

O comando cria apenas os dez arquivos em `samples/documents/` e `manifest.json` em `samples/ground_truth/`. Os caminhos padrão são relativos ao repositório. Para um diretório descartável, passe `--documents-dir` e `--ground-truth-dir`.

PDFs e PNG são reproduzíveis: o gerador usa metadados PDF invariantes, uma seed fixa para a rotação pequena da foto e copia os bytes do caso 01 para o 10. Se a saída já existir com bytes idênticos, ela é mantida; se algum arquivo diferir, o comando falha sem sobrescrevê-lo. Inspecione e mova/remova manualmente o arquivo conflitante antes de regenerar.

Valide o gerador e o manifesto com:

```bash
.venv/bin/python -m unittest backend.tests.test_demo_documents -v
```

Esses arquivos são entradas e gabaritos sintéticos; não demonstram que o modelo de extração acertou os campos. Execute a avaliação do provedor separadamente, com o gabarito mantido fora da entrada do modelo.

Para executar a avaliação com o pipeline real e um Ollama local, siga [`docs/avaliacao-extracao.md`](../docs/avaliacao-extracao.md). A execução exige `--run`, não baixa modelos e escreve artefatos brutos em `var/evaluations/`, ignorado pelo Git.
