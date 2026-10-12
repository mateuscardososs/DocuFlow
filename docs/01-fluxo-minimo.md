# DocuFlow — caso de referência do fluxo mínimo

Este documento fixa um único caso de sucesso para orientar o primeiro fluxo vertical. Os dados são fictícios. O arquivo de exemplo será criado como PDF com texto na etapa de implementação; por enquanto, o conteúdo abaixo é a fonte de verdade do gabarito.

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

**Arquivo:** `fornecedor_horizonte_doc-2026-1042.pdf`  
**Uma página, texto nativo, sem imposto, desconto ou frete.**

```text
METALÚRGICA HORIZONTE LTDA.
Identificador: FORN-0001
Documento comercial: DOC-2026-1042
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
    "raw_value": "DOC-2026-1042",
    "value": "DOC-2026-1042",
    "page": 1,
    "evidence": "Documento comercial: DOC-2026-1042"
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

## 5. Critérios de aceite do fluxo mínimo

- [ ] Um operador envia o PDF de exemplo associado ao pedido `PC-2026-0001`.
- [ ] A API valida tipo, leitura e limite de arquivo; rejeita arquivo inválido com mensagem compreensível.
- [ ] O sistema extrai o texto da página 1 sem OCR e preserva o texto por página.
- [ ] A extração estruturada valida contra o schema Pydantic e guarda valor original, valor normalizado e evidência.
- [ ] Se o JSON do modelo for inválido ou uma evidência não existir na página indicada, o documento não fica pronto para revisão e recebe um erro explícito.
- [ ] As regras usam `Decimal`, identificam a igualdade de fornecedor/pedido/itens e conferem os totais com tolerância de `R$ 0,01`.
- [ ] O documento aparece como `ready_for_review` com os dados extraídos, evidências, pedido associado e resultado das validações.
- [ ] O caso correto não é aprovado nem enviado ao mini ERP automaticamente.
- [ ] Testes automatizados desse fluxo usam um provedor de IA simulado; não exigem Ollama em execução.

## 6. Fora deste primeiro fluxo

Sem OCR, Redis/RQ, mini ERP, Playwright, interface React, autenticação completa ou múltiplos provedores. Essas partes entram em etapas posteriores, depois de validar este caminho básico.
