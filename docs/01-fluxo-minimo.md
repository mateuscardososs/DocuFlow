# DocuFlow — caso de referência do fluxo mínimo

Este documento fixa o caso comercial base do primeiro fluxo vertical. Todos os dados são fictícios. O arquivo de entrada atual é [`samples/documents/01_correto.pdf`](../samples/documents/01_correto.pdf); o conjunto de dez casos e seu manifesto reproduzível estão descritos em [`samples/README.md`](../samples/README.md). O manifesto, não este exemplo ilustrativo de JSON, registra os valores efetivamente impressos e os resultados comerciais calculados pelas regras atuais.

## 1. Pedido de compra

| Campo | Valor esperado |
|---|---|
| Referência | `PC-2026-0001` |
| Fornecedor | `Metalúrgica Horizonte Ltda.` |
| Identificador fictício | `FORN-0001` |
| Moeda | `BRL` |
| Item 1 | `ARR-001` — Arruela zincada M8, quantidade `10`, preço unitário `85.50`, total `855.00` |
| Item 2 | `PAR-002` — Parafuso sextavado M8, quantidade `5`, preço unitário `79.00`, total `395.00` |
| Total esperado | `1250.00` |

Valores monetários e quantidades são apresentados como strings decimais no contrato JSON para evitar conversões acidentais para ponto flutuante. No backend, os valores serão convertidos para `Decimal` antes dos cálculos.

## 2. Documento comercial fictício

**Arquivo:** `samples/documents/01_correto.pdf`

**Uma página, texto nativo, sem imposto, desconto ou frete.**

```text
DOCUMENTO FICTÍCIO PARA TESTES — SEM VALIDADE FISCAL
METALÚRGICA HORIZONTE LTDA.
Identificador: FORN-0001
Documento comercial: DOC-DEMO-001
Data de emissão: 10/10/2026
Pedido de compra: PC-2026-0001
Moeda: BRL

Código | Descrição                 | Quantidade | Preço unitário | Total
ARR-001 | Arruela zincada M8       | 10         | R$ 85,50       | R$ 855,00
PAR-002 | Parafuso sextavado M8    | 5          | R$ 79,00       | R$ 395,00

Total do documento: R$ 1.250,00
```

## 3. Contrato de extração

O modelo retorna JSON com valores originais e normalizados separados. `value` é o valor normalizado usado pelo backend; valores numéricos e monetários ficam como strings decimais. Se um dado estiver ausente, `raw_value`, `value`, `page` e `evidence` são `null`. O modelo não deve completar campos por inferência nem calcular totais ausentes.

```json
{
  "supplier_name": {
    "raw_value": "METALÚRGICA HORIZONTE LTDA.",
    "value": "Metalúrgica Horizonte Ltda.",
    "page": 1,
    "evidence": "METALÚRGICA HORIZONTE LTDA."
  },
  "supplier_identifier": {
    "raw_value": "FORN-0001",
    "value": "FORN-0001",
    "page": 1,
    "evidence": "Identificador: FORN-0001"
  },
  "document_number": {
    "raw_value": "DOC-DEMO-001",
    "value": "DOC-DEMO-001",
    "page": 1,
    "evidence": "Documento comercial: DOC-DEMO-001"
  },
  "issue_date": {
    "raw_value": "10/10/2026",
    "value": "2026-10-10",
    "page": 1,
    "evidence": "Data de emissão: 10/10/2026"
  },
  "purchase_order_reference": {
    "raw_value": "PC-2026-0001",
    "value": "PC-2026-0001",
    "page": 1,
    "evidence": "Pedido de compra: PC-2026-0001"
  },
  "currency": {
    "raw_value": "BRL",
    "value": "BRL",
    "page": 1,
    "evidence": "Moeda: BRL"
  },
  "items": [
    {
      "code": {"raw_value": "ARR-001", "value": "ARR-001", "page": 1, "evidence": "ARR-001"},
      "description": {"raw_value": "Arruela zincada M8", "value": "Arruela zincada M8", "page": 1, "evidence": "Arruela zincada M8"},
      "quantity": {"raw_value": "10", "value": "10", "page": 1, "evidence": "10"},
      "unit_price": {"raw_value": "R$ 85,50", "value": "85.50", "page": 1, "evidence": "R$ 85,50"},
      "line_total": {"raw_value": "R$ 855,00", "value": "855.00", "page": 1, "evidence": "R$ 855,00"}
    },
    {
      "code": {"raw_value": "PAR-002", "value": "PAR-002", "page": 1, "evidence": "PAR-002"},
      "description": {"raw_value": "Parafuso sextavado M8", "value": "Parafuso sextavado M8", "page": 1, "evidence": "Parafuso sextavado M8"},
      "quantity": {"raw_value": "5", "value": "5", "page": 1, "evidence": "5"},
      "unit_price": {"raw_value": "R$ 79,00", "value": "79.00", "page": 1, "evidence": "R$ 79,00"},
      "line_total": {"raw_value": "R$ 395,00", "value": "395.00", "page": 1, "evidence": "R$ 395,00"}
    }
  ],
  "document_total": {
    "raw_value": "R$ 1.250,00",
    "value": "1250.00",
    "page": 1,
    "evidence": "Total do documento: R$ 1.250,00"
  },
  "unsupported_elements": []
}
```

`unsupported_elements` registra termos reconhecidos como impostos, descontos ou frete. Se vier preenchido, o documento exige revisão e não segue como caso simples. Evidência localizada na página indicada comprova apenas que o trecho aparece no texto extraído; não prova que a interpretação está correta.

## 4. Regras de conferência deste caso

1. Todos os campos obrigatórios existem: fornecedor, identificador, número, data, pedido, moeda, itens e total.
2. Data normalizada é uma data válida ISO (`2026-10-10`).
3. Quantidades são positivas; preço unitário e total de item são valores `Decimal` não negativos.
4. Para cada item, quantidade × preço unitário é igual ao total do item, com tolerância de `R$ 0,01`.
5. A soma dos totais dos itens é igual ao total do documento, com tolerância de `R$ 0,01`.
6. Fornecedor, identificador, referência do pedido, códigos, quantidades e preços correspondem ao pedido cadastrado.
7. Moeda `BRL` corresponde à moeda do pedido.
8. Cada evidência aponta para a página 1 e pode ser localizada no texto extraído dessa página após normalização de espaços.
9. Com as regras satisfeitas e sem elementos não suportados, o resultado fica pronto para revisão humana; isso não aprova nem lança o documento automaticamente.

## 5. Status e evidências dos critérios

“Implementado” indica comportamento presente no código; “testado automaticamente” aponta cobertura automatizada. “Validação manual” informa se há registro no repositório. A edição desta documentação não executou serviços nem dados de aplicação.

| Critério | Implementado | Testado automaticamente | Validação manual registrada |
|---|---|---|---|
| Operador envia arquivo e pedido de referência | Sim, `POST /documents/analyze` e associação autenticada ao workspace | `test_full_reference_flow_returns_review_result_without_pdf_bytes`; `test_valid_upload_calls_processing_service_and_returns_typed_result` | Não consta neste repositório |
| Tipo, leitura, tamanho e arquivo inválido | Sim, API limita upload e extrator valida o documento | `test_unsupported_media_type_is_rejected_before_processing`; `test_oversized_upload_is_rejected_before_processing`; `test_invalid_pdf_content_returns_clear_client_error` | Não consta neste repositório |
| PDF nativo separado por página | Sim; páginas preservam texto e origem | `test_extracts_reference_document_text_on_page_one`; `test_keeps_text_split_in_page_order` | Não consta neste repositório |
| Schema estrito, valores originais/normalizados e evidência | Sim; campos passam pelo schema Pydantic e a evidência é conferida na página indicada | `test_valid_response_uses_existing_schema_and_business_validation`; `test_schema_rejects_unexpected_properties_and_numeric_values`; `test_evidence_must_appear_on_indicated_page` | Não consta neste repositório |
| Campos obrigatórios, data, dinheiro, quantidades e totais | Sim; cálculos monetários usam `Decimal` e tolerância padrão de `0.01` | `test_reference_case_is_ready_for_human_review`; `test_missing_required_field_is_blocking`; `test_invalid_date_is_blocking`; `test_incorrect_line_total_is_divergence`; `test_incorrect_document_total_is_divergence` | Não consta neste repositório |
| Resultado pronto para revisão, sem aprovação automática | Sim; processamento e revisão são estados distintos | `test_full_reference_flow_returns_review_result_without_pdf_bytes`; `test_commercial_divergence_is_returned_without_approval` | Não consta neste repositório |
| Teste sem serviço de IA obrigatório | Sim; os serviços aceitam provedor injetado | `test_extraction_provider_can_be_injected_without_ollama`; `test_valid_response_uses_existing_schema_and_business_validation` | Não consta neste repositório |

O workflow Playwright automatiza etapas de login, upload, revisão, aprovação e envio simulado em Chromium, mas continua sendo teste automatizado da aplicação. Ele não constitui validação manual de usuário nem valida ERP externo ou emissão fiscal. A configuração de VPS também não equivale a deploy público validado.

## 6. Escopo atual e limitações

OCR local, autenticação, Redis/RQ, interface React, revisão humana, Playwright, auditoria, painel, importação CSV, atribuição, regras versionadas e adaptador ERP demonstrativo foram adicionados depois do fluxo mínimo e estão descritos em [funcionalidades e contratos](funcionalidades.md).

O ERP permanece simulado por `DemoERPClient`: não há emissão fiscal, RPA contra ERP externo ou reconciliação depois de lançamento externo. O avaliador permite executar uma avaliação manual com modelo local sobre a amostra sintética; os testes automatizados usam provedores determinísticos e não demonstram taxa de acerto geral ou em produção.
