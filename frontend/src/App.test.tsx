import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
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

const validation = { status: 'ready_for_review', findings: [] }

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
} = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = []
  let currentDetail = options.detail ?? documentDetail()
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    calls.push({ url, init })
    if (url.endsWith('/auth/token')) return json({ access_token: 'test-bearer', token_type: 'bearer', expires_in: 1800 })
    if (url.endsWith('/auth/me')) return json(profile(options.roles, options.multiWorkspace))
    if (url.endsWith('/documents?limit=100&offset=0')) return json([documentSummary()])
    if (url.endsWith('/documents/analyze')) return json({
      document_id: 22, duplicate: false, processing_status: 'queued', state: 'queued',
      review_status: 'pending', review_version: 1, processing_error: null,
      document: null, pdf_warnings: null, validation: null, status: null,
    }, 202)
    if (url.endsWith('/documents/21/audit')) return json(options.erpEvent ? [{
      id: 92, event_type: 'erp_submission', version: 1, actor_user_id: 7,
      justification: null, details: { submission_id: 81 },
      created_at: '2026-10-13T12:00:00Z',
    }] : [{
      id: 91, event_type: 'review', version: 1, actor_user_id: 7,
      justification: 'Documento recebido para revisão', details: null,
      created_at: '2026-10-13T12:00:00Z',
    }])
    if (url.endsWith('/documents/21/download')) return new Response(new Blob(['original']), { status: 200 })
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
    expect(await screen.findByText(/Acme Papelaria NF-42/)).toBeInTheDocument()
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
    expect(await screen.findByText('Texto nativo')).toBeInTheDocument()
    expect(await screen.findByText('Revisão')).toBeInTheDocument()
    expect(screen.getByText(/Acme Papelaria NF-42/)).toBeInTheDocument()
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await user.click(screen.getByRole('button', { name: '↓ Baixar original' }))
    const downloadCall = calls.find((call) => call.url.endsWith('/documents/21/download'))
    const headers = new Headers(downloadCall?.init.headers)
    expect(headers.get('Authorization')).toBe('Bearer test-bearer')
    expect(headers.get('X-Workspace-ID')).toBe('12')
  })
})

async function waitForWorkspaceHeader(calls: Array<{ url: string; init: RequestInit }>, workspaceId: string) {
  await vi.waitFor(() => {
    const listCall = calls.find((call) => call.url.includes('/documents?') && new Headers(call.init.headers).get('X-Workspace-ID') === workspaceId)
    expect(listCall).toBeDefined()
  })
}
