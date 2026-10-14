import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'

const profile = (roles: Array<'operator' | 'approver'> = ['operator', 'approver'], multiWorkspace = false) => ({
  id: 7,
  email: 'ana@example.test',
  workspaces: [
    { id: 12, slug: 'financeiro', name: 'Financeiro', roles },
    ...(multiWorkspace ? [{ id: 14, slug: 'operacoes', name: 'Operações', roles: ['operator' as const] }] : []),
  ],
})

const field = (value: string, page = 1) => ({
  raw_value: value,
  value,
  page,
  evidence: value,
})

const referenceDocument = () => ({
  supplier_name: field('Acme Papelaria'),
  supplier_identifier: field('12345678000190'),
  document_number: field('NF-42'),
  issue_date: field('2026-10-01'),
  purchase_order_reference: field('PC-2026-0001'),
  currency: field('BRL'),
  items: [{
    code: field('PAP-01'),
    description: field('Papel A4'),
    quantity: field('2'),
    unit_price: field('10.00'),
    line_total: field('20.00'),
  }],
  document_total: field('20.00'),
  unsupported_elements: [],
})

const validation = { status: 'ready_for_review', findings: [], commercial_policy: { version: 0, default_total_tolerance: '0.01', currency_tolerances: {} } }

const documentSummary = (overrides: Record<string, unknown> = {}) => ({
  id: 21,
  workspace_id: 12,
  purchase_order_id: 3,
  purchase_order_reference: 'PC-2026-0001',
  supplier_name: 'Acme Papelaria',
  original_filename: 'nota.pdf',
  size_bytes: 1234,
  content_type: 'application/pdf',
  sha256: 'abc',
  uploaded_at: '2026-10-13T12:00:00Z',
  processing_status: 'ready_for_review',
  review_status: 'pending',
  review_version: 1,
  processing_error: null,
  final_status: 'ready_for_review',
  assignee_user_id: null,
  assignee_email: null,
  assigned_by_user_id: null,
  assigned_by_email: null,
  assigned_at: null,
  assignment_status: 'unassigned',
  ...overrides,
})

const documentDetail = (overrides: Record<string, unknown> = {}) => ({
  ...documentSummary(),
  page_texts: [{ number: 1, text: 'Acme Papelaria NF-42 PC-2026-0001', source: 'native' }],
  extracted_document: referenceDocument(),
  reviewed_document: null,
  pdf_warnings: [],
  validation_result: validation,
  approval: null,
  ...overrides,
})

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

function mockApi(options: {
  roles?: Array<'operator' | 'approver'>
  multiWorkspace?: boolean
  detail?: Record<string, unknown>
  conflict?: boolean
  approved?: boolean
  erpEvent?: boolean
  downloadStatus?: number
  downloadType?: string
  documents?: Array<Record<string, unknown>>
  members?: Array<{ id: number; email: string; roles: string[] }>
  dashboardFailure?: boolean
  policyConflict?: boolean
} = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = []
  let currentDetail = options.detail ?? documentDetail()
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    calls.push({ url, init })
    if (url.endsWith('/auth/token')) return json({ access_token: 'test-bearer', token_type: 'bearer', expires_in: 1800 })
    if (url.endsWith('/auth/me')) return json(profile(options.roles, options.multiWorkspace))
    if (url.includes('/dashboard?')) {
      if (options.dashboardFailure) return json({ detail: 'Indisponível' }, 503)
      return json({
        start_date: '2026-10-01', end_date: '2026-10-14',
        documents: { queued: 2, processing: 1, awaiting_review: 3, awaiting_approval: 1, processing_failed: 1 },
        erp_submissions: { pending: 2, succeeded: 4, failed: 1 },
        volume_total: 5,
        document_volume: [{ date: '2026-10-13', count: 2 }, { date: '2026-10-14', count: 3 }],
        common_divergences: [{ code: 'price_mismatch', count: 2 }],
      })
    }
    if (url.endsWith('/commercial-policy/history')) return json([
      { version: 1, default_total_tolerance: '0.01', currency_tolerances: { BRL: '0.05' }, created_by_user_id: 7, created_by_email: 'ana@example.test', created_at: '2026-10-13T12:00:00Z' },
    ])
    if (url.endsWith('/commercial-policy') && init.method === 'PUT') {
      if (options.policyConflict) return json({ detail: 'A política mudou desde a leitura. Atualize os dados antes de salvar.' }, 409)
      const body = JSON.parse(String(init.body)) as { expected_version: number; currency_tolerances: Record<string, string> }
      return json({ version: body.expected_version + 1, default_total_tolerance: '0.01', currency_tolerances: body.currency_tolerances, created_by_user_id: 7, created_by_email: 'ana@example.test', created_at: '2026-10-14T12:00:00Z' })
    }
    if (url.endsWith('/commercial-policy')) return json({ version: 1, default_total_tolerance: '0.01', currency_tolerances: { BRL: '0.05' }, created_by_user_id: 7, created_by_email: 'ana@example.test', created_at: '2026-10-13T12:00:00Z' })
    if (url.includes('/documents?')) {
      let rows = options.documents ?? [documentSummary()]
      if (url.includes('assigned_to_me=true')) rows = rows.filter((row) => row.assignee_user_id === 7)
      if (url.includes('unassigned=true')) rows = rows.filter((row) => row.assignee_user_id === null)
      return json(rows)
    }
    if (url.endsWith('/workspaces/12/members')) return json(options.members ?? [{ id: 7, email: 'ana@example.test', roles: ['operator', 'approver'] }])
    if (url.endsWith('/documents/analyze')) return json({
      document_id: 22, duplicate: false, processing_status: 'queued', state: 'queued',
      review_status: 'pending', review_version: 1, processing_error: null,
      document: null, pdf_warnings: null, validation: null, status: null,
    }, 202)
    if (url.endsWith('/purchase-orders/import/preview')) return json({
      total_rows: 1,
      valid_rows: 1,
      orders: [{
        reference: 'PC-CSV-77', supplier_identifier: 'FORN-0001',
        supplier_name: 'Metalúrgica Horizonte Ltda.', currency: 'BRL', total: '20.00', item_count: 1,
        line_numbers: [2], outcome: 'new',
      }],
      errors: [],
    })
    if (url.endsWith('/purchase-orders/import/confirm')) return json({
      total_rows: 1, valid_rows: 1, imported: ['PC-CSV-77'],
      ignored_duplicates: [], errors: [],
    })
    if (url.endsWith('/documents/21/audit')) return json(options.erpEvent ? [{
      id: 92, event_type: 'erp_submission', version: 1, actor_user_id: 7,
      justification: null, details: { submission_id: 81 },
      created_at: '2026-10-13T12:00:00Z',
    }] : [{
      id: 91, event_type: 'review', version: 1, actor_user_id: 7,
      justification: 'Documento recebido para revisão', details: null,
      created_at: '2026-10-13T12:00:00Z',
    }])
    if (url.includes('/documents/21/download')) {
      if (options.downloadStatus && options.downloadStatus !== 200) {
        return json({ detail: options.downloadStatus === 403 ? 'Acesso ao arquivo negado.' : 'Arquivo temporariamente indisponível.' }, options.downloadStatus)
      }
      return new Response(new Blob(['original'], { type: options.downloadType ?? 'application/pdf' }), { status: 200 })
    }
    if (url.endsWith('/documents/21/assignment')) {
      const request = JSON.parse(String(init.body)) as { assignee_user_id: number | null }
      const member = options.members?.find((item) => item.id === request.assignee_user_id)
      currentDetail = {
        ...currentDetail,
        assignee_user_id: request.assignee_user_id,
        assignee_email: member?.email ?? null,
        assigned_by_user_id: 7,
        assigned_by_email: 'ana@example.test',
        assigned_at: '2026-10-14T12:00:00Z',
        assignment_status: request.assignee_user_id === null ? 'unassigned' : 'assigned',
      }
      return json(currentDetail)
    }
    if (url.endsWith('/documents/21')) return json(currentDetail)
    if (url.endsWith('/documents/21/review')) {
      if (options.conflict) return json({ detail: 'A versão do documento mudou.' }, 409)
      return json({ detail: documentDetail({ reviewed_document: referenceDocument(), review_version: 2 }) })
    }
    if (url.endsWith('/documents/21/approve')) {
      if (options.approved) {
        currentDetail = documentDetail({
          review_status: 'approved',
          approval: { approved_version: 1, approved_document: referenceDocument(), validation_result: validation },
        })
        return json({ detail: currentDetail })
      }
      return json({ detail: 'Documento aprovado.' })
    }
    if (url.endsWith('/documents/21/reject')) {
      currentDetail = documentDetail({ review_status: 'rejected' })
      return json({ detail: 'Documento rejeitado.' })
    }
    if (url.endsWith('/documents/21/erp-submission')) return json({
      id: 81, document_id: 21, approved_version: 1, state: 'queued', attempts: 0,
      erp_identifier: null, failure_message: null,
      requested_at: '2026-10-13T12:00:00Z', updated_at: '2026-10-13T12:00:00Z',
    }, 202)
    if (url.endsWith('/erp-submissions/81')) return json({
      id: 81, document_id: 21, approved_version: 1, state: 'succeeded', attempts: 1,
      erp_identifier: 'ERP-DEMO-123', failure_message: null,
      requested_at: '2026-10-13T12:00:00Z', updated_at: '2026-10-13T12:01:00Z',
    })
    throw new Error(`Unexpected API request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock, setDetail: (value: Record<string, unknown>) => { currentDetail = value } }
}

async function signIn() {
  const user = userEvent.setup()
  render(<App />)
  await user.type(screen.getByLabelText('E-mail'), 'ana@example.test')
  await user.type(screen.getByLabelText('Senha'), 'password-123')
  await user.click(screen.getByRole('button', { name: 'Entrar' }))
  await screen.findByRole('heading', { name: 'Documentos' })
  return user
}

describe('DocuFlow web app', () => {
  beforeEach(() => vi.stubEnv('VITE_API_BASE_URL', 'http://api.test'))
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  it('faz login, escolhe workspace e envia token e workspace no cliente HTTP', async () => {
    const { calls } = mockApi({ multiWorkspace: true })
    await signIn()
    expect(await screen.findByRole('cell', { name: /Acme Papelaria/ })).toBeInTheDocument()
    const listCall = calls.find((call) => call.url.includes('/documents?'))
    const listHeaders = new Headers(listCall?.init.headers)
    expect(listHeaders.get('Authorization')).toBe('Bearer test-bearer')
    expect(listHeaders.get('X-Workspace-ID')).toBe('12')
    expect(screen.getByLabelText('Workspace')).toHaveValue('12')
    await userEvent.setup().selectOptions(screen.getByLabelText('Workspace'), '14')
    await waitForWorkspaceHeader(calls, '14')
  })

  it('mostra indicadores agregados e envia workspace e período ao painel', async () => {
    const { calls } = mockApi({ multiWorkspace: true })
    const user = await signIn()
    await user.click(screen.getByRole('button', { name: 'Painel operacional' }))
    expect(await screen.findByRole('heading', { name: 'Painel operacional' })).toBeInTheDocument()
    expect(screen.getByText('Aguardando revisão')).toBeInTheDocument()
    expect(screen.getByText('price mismatch')).toBeInTheDocument()
    const request = calls.find((call) => call.url.includes('/dashboard?'))
    expect(request?.url).toContain('start_date=')
    expect(request?.url).toContain('end_date=')
    expect(new Headers(request?.init.headers).get('X-Workspace-ID')).toBe('12')
    const startInput = screen.getByLabelText('Início do período do painel') as HTMLInputElement
    const changedStart = new Date(`${startInput.value}T12:00:00`)
    changedStart.setDate(changedStart.getDate() + 1)
    const changedDate = `${changedStart.getFullYear()}-${String(changedStart.getMonth() + 1).padStart(2, '0')}-${String(changedStart.getDate()).padStart(2, '0')}`
    fireEvent.change(startInput, { target: { value: changedDate } })
    await waitFor(() => expect(calls.some((call) => call.url.includes(`start_date=${changedDate}`))).toBe(true))
    await user.selectOptions(screen.getByLabelText('Workspace'), '14')
    await waitFor(() => expect(calls.some((call) => call.url.includes('/dashboard?') && new Headers(call.init.headers).get('X-Workspace-ID') === '14')).toBe(true))
  })

  it('mostra estado de erro se o agregado do painel falhar', async () => {
    mockApi({ dashboardFailure: true })
    const user = await signIn()
    await user.click(screen.getByRole('button', { name: 'Painel operacional' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Indisponível')
  })

  it('consulta, altera e exibe o histórico versionado da política do workspace', async () => {
    const { calls } = mockApi()
    const user = await signIn()
    await user.click(screen.getByRole('button', { name: 'Regras comerciais' }))
    expect(await screen.findByRole('heading', { name: 'Tolerâncias de totais' })).toBeInTheDocument()
    expect(screen.getByText('Versão 1')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '＋ Adicionar moeda' }))
    await user.type(screen.getByLabelText('Moeda 2'), 'USD')
    await user.type(screen.getByLabelText('Tolerância 2'), '0.25')
    await user.click(screen.getByRole('button', { name: 'Salvar nova versão (v2)' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Nova versão das regras comerciais salva.')
    const update = calls.find((call) => call.url.endsWith('/commercial-policy') && call.init.method === 'PUT')
    expect(new Headers(update?.init.headers).get('X-Workspace-ID')).toBe('12')
    expect(JSON.parse(String(update?.init.body))).toEqual({ expected_version: 1, currency_tolerances: { BRL: '0.05', USD: '0.25' } })
  })

  it('oculta a configuração de regras para quem só pode aprovar', async () => {
    mockApi({ roles: ['approver'] })
    await signIn()
    expect(screen.queryByRole('button', { name: 'Regras comerciais' })).not.toBeInTheDocument()
  })

  it('avisa sobre conflito de política quando uma versão mais nova já foi salva', async () => {
    mockApi({ policyConflict: true })
    const user = await signIn()
    await user.click(screen.getByRole('button', { name: 'Regras comerciais' }))
    await screen.findByRole('heading', { name: 'Tolerâncias de totais' })
    await user.click(screen.getByRole('button', { name: '＋ Adicionar moeda' }))
    await user.type(screen.getByLabelText('Moeda 2'), 'USD')
    await user.type(screen.getByLabelText('Tolerância 2'), '0.25')
    await user.click(screen.getByRole('button', { name: 'Salvar nova versão (v2)' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('A política mudou desde a leitura.')
    expect(screen.getByRole('button', { name: 'Recarregar política' })).toBeInTheDocument()
  })

  it('permite upload a operador e mostra o estado enfileirado', async () => {
    const { calls } = mockApi({ roles: ['operator'] })
    const user = await signIn()
    await user.type(screen.getByLabelText('Referência do pedido'), 'PC-2026-0001')
    const fileInput = screen.getByLabelText('Arquivo PDF, PNG ou JPEG') as HTMLInputElement
    await user.upload(fileInput, new File(['pdf'], 'nota.pdf', { type: 'application/pdf' }))
    expect(fileInput.files?.[0]?.name).toBe('nota.pdf')
    expect((screen.getByLabelText('Referência do pedido') as HTMLInputElement).value).toBe('PC-2026-0001')
    expect(fileInput.form?.checkValidity()).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Enviar para análise' }))
    expect(await screen.findByText(/Documento #22.*na fila/i)).toBeInTheDocument()
    expect(calls.some((call) => call.url.endsWith('/documents/analyze'))).toBe(true)
  })

  it('bloqueia ações de operador para usuário com apenas papel approver', async () => {
    mockApi({ roles: ['approver'] })
    await signIn()
    expect(screen.queryByRole('button', { name: 'Enviar para análise' })).not.toBeInTheDocument()
    expect(screen.getByText(/Seu papel aprovador/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Importar pedidos' })).not.toBeInTheDocument()
  })

  it('pré-visualiza e confirma CSV com token e workspace selecionado', async () => {
    const { calls } = mockApi({ roles: ['operator'] })
    const user = await signIn()
    await user.click(screen.getByRole('button', { name: 'Importar pedidos' }))
    const input = screen.getByLabelText('Arquivo CSV') as HTMLInputElement
    await user.upload(input, new File([
      'purchase_order_reference,supplier_identifier,currency,order_total,item_code,item_description,quantity,unit_price,line_total\n' +
      'PC-CSV-77,FORN-0001,BRL,20.00,ITEM-1,Peça,2,10.00,20.00',
    ], 'pedidos.csv', { type: 'text/csv' }))
    await user.click(screen.getByRole('button', { name: 'Gerar prévia' }))
    expect(await screen.findByText('PC-CSV-77')).toBeInTheDocument()
    expect(screen.getByText('Será importado')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Confirmar importação' }))
    expect(await screen.findByText(/1 pedidos novos e 0 duplicatas ignoradas/)).toBeInTheDocument()
    const requests = calls.filter((call) => call.url.includes('/purchase-orders/import/'))
    expect(requests).toHaveLength(2)
    for (const request of requests) {
      const headers = new Headers(request.init.headers)
      expect(headers.get('Authorization')).toBe('Bearer test-bearer')
      expect(headers.get('X-Workspace-ID')).toBe('12')
      expect(request.init.body).toBeInstanceOf(FormData)
    }
  })

  it('filtra documentos por estado, fornecedor, busca e data', async () => {
    mockApi()
    const user = await signIn()
    await user.selectOptions(screen.getByLabelText('Filtrar por estado'), 'failed')
    expect(await screen.findByText('Nenhum documento encontrado')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Filtrar por estado'), 'review')
    expect(screen.getByRole('button', { name: /Abrir nota\.pdf/ })).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Filtrar por fornecedor'), 'Acme Papelaria')
    expect(screen.getByRole('button', { name: /Abrir nota\.pdf/ })).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText('Buscar arquivo, fornecedor ou pedido'), 'não-encontrado')
    expect(screen.getByText('Nenhum documento encontrado')).toBeInTheDocument()
    await user.clear(screen.getByPlaceholderText('Buscar arquivo, fornecedor ou pedido'))
    await user.type(screen.getByLabelText('Data inicial'), '2026-10-14')
    expect(screen.getByText('Nenhum documento encontrado')).toBeInTheDocument()
  })

  it('mostra conflito de versão e permite recarregar o documento', async () => {
    const { calls } = mockApi({ roles: ['operator'], conflict: true })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    await user.click(await screen.findByRole('button', { name: 'Editar dados' }))
    await user.type(screen.getByLabelText('Justificativa da correção'), 'Ajuste conferido na fonte')
    await user.clear(screen.getByLabelText('Número do documento valor'))
    await user.type(screen.getByLabelText('Número do documento valor'), 'NF-43')
    await user.click(screen.getByRole('button', { name: '＋ Adicionar item' }))
    expect(screen.getByLabelText('Item 2 · Código valor')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Remover item 2' }))
    await user.click(screen.getByRole('button', { name: 'Salvar revisão' }))
    expect(await screen.findByText(/documento mudou/i)).toBeInTheDocument()
    const correction = calls.find((call) => call.url.endsWith('/documents/21/review'))
    expect(JSON.parse(String(correction?.init.body))).toMatchObject({
      expected_version: 1,
      justification: 'Ajuste conferido na fonte',
      reviewed_document: { document_number: { value: 'NF-43' } },
    })
    await user.click(screen.getByRole('button', { name: 'Recarregar documento' }))
    expect(screen.getByLabelText('Texto extraído da página 1').textContent).toMatch(/Acme Papelaria NF-42/)
  })

  it('mostra a versão da política comercial aplicada no detalhe', async () => {
    mockApi()
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    expect(await screen.findByText('Política v0')).toBeInTheDocument()
  })

  it('aprova explicitamente e solicita envio ao ERP somente depois da aprovação', async () => {
    const { calls } = mockApi({ roles: ['approver'], approved: true })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    await user.click(await screen.findByRole('button', { name: /Aprovar documento/ }))
    await user.click(await screen.findByRole('button', { name: 'Enviar ao ERP de demonstração' }))
    expect(await screen.findByText('Na fila')).toBeInTheDocument()
    const erpRequest = calls.find((call) => call.url.endsWith('/documents/21/erp-submission'))
    const erpHeaders = new Headers(erpRequest?.init.headers)
    expect(erpHeaders.get('Authorization')).toBe('Bearer test-bearer')
    expect(erpHeaders.get('X-Workspace-ID')).toBe('12')
    expect(JSON.parse(String(erpRequest?.init.body))).toEqual({ expected_version: 1 })
  })

  it('exige justificativa para rejeitar e não executa a ação com campo vazio', async () => {
    const { calls } = mockApi({ roles: ['approver'] })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    await user.click(await screen.findByRole('button', { name: 'Rejeitar documento' }))
    expect(await screen.findByText('Informe uma justificativa para rejeitar.')).toBeInTheDocument()
    expect(calls.some((call) => call.url.endsWith('/documents/21/reject'))).toBe(false)
  })

  it('envia justificativa e versão esperada ao rejeitar', async () => {
    const { calls } = mockApi({ roles: ['approver'] })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    await user.type(screen.getByLabelText('Motivo da rejeição'), 'Dados incompatíveis com o pedido')
    await user.click(await screen.findByRole('button', { name: 'Rejeitar documento' }))
    const rejection = calls.find((call) => call.url.endsWith('/documents/21/reject'))
    expect(rejection).toBeDefined()
    expect(JSON.parse(String(rejection?.init.body))).toEqual({
      expected_version: 1,
      justification: 'Dados incompatíveis com o pedido',
    })
  })

  it('recupera o estado ERP persistido pelo evento de auditoria ao abrir documento aprovado', async () => {
    mockApi({
      roles: ['approver'],
      erpEvent: true,
      detail: documentDetail({
        review_status: 'approved',
        approval: { approved_version: 1, approved_document: referenceDocument(), validation_result: validation },
      }),
    })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    expect(await screen.findByText('ERP-DEMO-123')).toBeInTheDocument()
    expect(screen.getByText('Cadastro simulado concluído')).toBeInTheDocument()
    expect(screen.getByText('Tentativas: 1')).toBeInTheDocument()
  })

  it('mostra texto por página e auditoria e baixa o original com token e workspace', async () => {
    const { calls } = mockApi({ roles: ['operator'] })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    expect(await screen.findByText('native')).toBeInTheDocument()
    expect(await screen.findByText('Revisão')).toBeInTheDocument()
    expect(screen.getByLabelText('Texto extraído da página 1').textContent).toMatch(/Acme Papelaria NF-42/)
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await user.click(screen.getByRole('button', { name: '↓ Baixar original' }))
    const downloadCall = calls.find((call) => call.url.endsWith('/documents/21/download'))
    const headers = new Headers(downloadCall?.init.headers)
    expect(headers.get('Authorization')).toBe('Bearer test-bearer')
    expect(headers.get('X-Workspace-ID')).toBe('12')
  })

  it('sincroniza o PDF de várias páginas com a página da evidência selecionada', async () => {
    vi.stubGlobal('URL', Object.assign(class URLMock {}, URL, {
      createObjectURL: vi.fn(() => 'blob:docuflow-original'),
      revokeObjectURL: vi.fn(),
    }))
    const detail = documentDetail({
      page_texts: [
        { number: 1, text: 'Página inicial da nota.', source: 'native' },
        { number: 2, text: 'Acme Papelaria · total 20,00', source: 'ocr' },
      ],
      extracted_document: {
        ...referenceDocument(),
        supplier_name: field('Acme Papelaria', 2),
        document_total: field('20,00', 2),
      },
    })
    const { calls } = mockApi({ roles: ['operator'], detail, downloadType: 'application/pdf' })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))

    const viewer = await screen.findByTitle('Documento original, página 1')
    expect(viewer).toHaveAttribute('src', 'blob:docuflow-original#page=1')
    await user.click(screen.getByRole('button', { name: /Ver evidência de Fornecedor/ }))
    expect(await screen.findByTitle('Documento original, página 2')).toHaveAttribute('src', 'blob:docuflow-original#page=2')
    expect(screen.getByText('Acme Papelaria', { selector: 'mark' })).toBeInTheDocument()
    expect(screen.getByText('OCR')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Página anterior' }))
    expect(await screen.findByTitle('Documento original, página 1')).toHaveAttribute('src', 'blob:docuflow-original#page=1')
    await user.click(screen.getByRole('button', { name: 'Próxima página' }))
    expect(await screen.findByTitle('Documento original, página 2')).toHaveAttribute('src', 'blob:docuflow-original#page=2')
    const downloadCall = calls.find((call) => call.url.includes('/documents/21/download?inline=true'))
    const headers = new Headers(downloadCall?.init.headers)
    expect(headers.get('Authorization')).toBe('Bearer test-bearer')
    expect(headers.get('X-Workspace-ID')).toBe('12')
  })

  it('mostra PNG como imagem e sincroniza a navegação com a evidência', async () => {
    vi.stubGlobal('URL', Object.assign(class URLMock {}, URL, {
      createObjectURL: vi.fn(() => 'blob:docuflow-image'),
      revokeObjectURL: vi.fn(),
    }))
    const detail = documentDetail({
      content_type: 'image/png',
      page_texts: [{ number: 1, text: 'Documento escaneado', source: 'ocr' }],
    })
    mockApi({ roles: ['operator'], detail, downloadType: 'image/png' })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    expect(await screen.findByRole('img', { name: 'Página 1 do documento original' })).toHaveAttribute('src', 'blob:docuflow-image')
    expect(screen.getByText('OCR')).toBeInTheDocument()
  })

  it('informa quando a evidência não aparece no texto e mantém o arquivo protegido', async () => {
    vi.stubGlobal('URL', Object.assign(class URLMock {}, URL, {
      createObjectURL: vi.fn(() => 'blob:docuflow-original'),
      revokeObjectURL: vi.fn(),
    }))
    const detail = documentDetail({
      page_texts: [{ number: 1, text: 'Texto diferente na página.', source: 'native' }],
      extracted_document: { ...referenceDocument(), supplier_name: field('Acme Papelaria', 1) },
    })
    mockApi({ roles: ['operator'], detail, downloadStatus: 403 })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Acesso ao arquivo negado.')
    expect(screen.getByText(/evidência não localizada no texto extraído/i)).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /documento original/ })).not.toBeInTheDocument()
  })

  it('mostra estado de carregamento e opção de tentar novamente após falha do arquivo', async () => {
    vi.stubGlobal('URL', Object.assign(class URLMock {}, URL, {
      createObjectURL: vi.fn(() => 'blob:docuflow-original'),
      revokeObjectURL: vi.fn(),
    }))
    mockApi({ roles: ['operator'], downloadStatus: 503 })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Arquivo temporariamente indisponível.')
    await user.click(screen.getByRole('button', { name: 'Tentar carregar arquivo novamente' }))
  })

  it('filtra a fila por documentos atribuídos ao usuário e sem responsável', async () => {
    const docs = [
      documentSummary({ assignee_user_id: 7, assignee_email: 'ana@example.test', assignment_status: 'assigned' }),
      documentSummary({ id: 22, original_filename: 'sem-responsavel.pdf' }),
    ]
    const { calls } = mockApi({ documents: docs })
    const user = await signIn()
    expect(await screen.findByRole('button', { name: 'Abrir sem-responsavel.pdf' })).toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filtrar por responsável' }), 'mine')
    expect(await screen.findByRole('button', { name: 'Abrir nota.pdf' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Abrir sem-responsavel.pdf' })).not.toBeInTheDocument()
    expect(calls.some((call) => call.url.includes('assigned_to_me=true'))).toBe(true)
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filtrar por responsável' }), 'unassigned')
    expect(await screen.findByRole('button', { name: 'Abrir sem-responsavel.pdf' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Abrir nota.pdf' })).not.toBeInTheDocument()
    expect(calls.some((call) => call.url.includes('unassigned=true'))).toBe(true)
  })

  it('permite que operador atribua o documento a um membro e mostra a autoria', async () => {
    const { calls } = mockApi({
      members: [
        { id: 7, email: 'ana@example.test', roles: ['operator'] },
        { id: 9, email: 'joao@example.test', roles: ['approver'] },
      ],
    })
    const user = await signIn()
    await user.click(await screen.findByRole('button', { name: 'Abrir nota.pdf' }))
    expect((await screen.findAllByText('Sem responsável'))[0]).toBeInTheDocument()
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Responsável' }), '9')
    await user.click(screen.getByRole('button', { name: 'Salvar atribuição' }))
    expect(await screen.findByText('joao@example.test')).toBeInTheDocument()
    expect(screen.getByText(/Atribuído por ana@example.test/)).toBeInTheDocument()
    const assignment = calls.find((call) => call.url.endsWith('/documents/21/assignment'))
    expect(assignment?.init.method).toBe('PUT')
    expect(JSON.parse(String(assignment?.init.body))).toEqual({ assignee_user_id: 9 })
  })
})

async function waitForWorkspaceHeader(calls: Array<{ url: string; init: RequestInit }>, workspaceId: string) {
  await vi.waitFor(() => {
    const listCall = calls.find((call) => call.url.includes('/documents?') && new Headers(call.init.headers).get('X-Workspace-ID') === workspaceId)
    expect(listCall).toBeDefined()
  })
}
