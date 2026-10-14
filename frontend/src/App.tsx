import { useCallback, useEffect, useMemo, useState, useId, useRef, type FormEvent } from 'react'
import { ApiError, DocuFlowApi } from './api'
import type {
  AuditEvent,
  CommercialPolicy,
  DocumentDetail,
  DocumentSummary,
  ExtractedField,
  ERPSubmission,
  PurchaseOrderImportConfirmation,
  PurchaseOrderImportPreview,
  ReferenceDocument,
  UserProfile,
  WorkspaceAccess,
  WorkspaceDashboard,
  WorkspaceMember,
} from './types'
import './App.css'

interface Session {
  token: string
  profile: UserProfile
}

const processingLabels: Record<DocumentSummary['processing_status'], string> = {
  queued: 'Na fila',
  processing: 'Processando',
  ready_for_review: 'Processado',
  failed: 'Falhou',
}

const reviewLabels: Record<DocumentSummary['review_status'], string> = {
  pending: 'Revisão necessária',
  approved: 'Aprovado',
  rejected: 'Rejeitado',
}

function reviewLabelFor(document: DocumentSummary): string {
  if (document.review_status !== 'pending') return reviewLabels[document.review_status]
  if (document.processing_status === 'ready_for_review') return 'Revisão necessária'
  if (document.processing_status === 'failed') return 'Aguardando reprocessamento'
  return 'Após processamento'
}

const eventLabels: Record<AuditEvent['event_type'], string> = {
  review: 'Revisão',
  correction: 'Correção',
  approval: 'Aprovação',
  rejection: 'Rejeição',
  erp_submission: 'Solicitação ao ERP',
  erp_attempt: 'Tentativa no ERP',
  erp_success: 'Cadastro simulado no ERP',
  erp_failure: 'Falha no ERP',
  assignment: 'Atribuição',
}

const fieldLabels: Array<[keyof Omit<ReferenceDocument, 'items' | 'unsupported_elements'>, string]> = [
  ['supplier_name', 'Fornecedor'],
  ['supplier_identifier', 'Identificador do fornecedor'],
  ['document_number', 'Número do documento'],
  ['issue_date', 'Data de emissão'],
  ['purchase_order_reference', 'Referência do pedido'],
  ['currency', 'Moeda'],
  ['document_total', 'Total do documento'],
]

function messageFrom(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return 'Não foi possível concluir a operação. Tente novamente.'
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('pt-BR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function fileSize(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function displayValue(value: string | null | undefined): string {
  return value?.trim() || 'Não identificado'
}

function emptyItem(): ReferenceDocument['items'][number] {
  const field = (): ExtractedField => ({ raw_value: null, value: null, page: null, evidence: null })
  return { code: field(), description: field(), quantity: field(), unit_price: field(), line_total: field() }
}

function isPending(status: DocumentSummary['processing_status']): boolean {
  return status === 'queued' || status === 'processing'
}

function fieldsOf(document: ReferenceDocument | null): Array<{ label: string; field: ExtractedField }> {
  if (!document) return []
  const fields = fieldLabels.map(([key, label]) => ({ label, field: document[key] }))
  document.items.forEach((item, index) => {
    ;([['code', 'Código'], ['description', 'Descrição'], ['quantity', 'Quantidade'], ['unit_price', 'Preço unitário'], ['line_total', 'Total do item']] as const)
      .forEach(([key, label]) => fields.push({ label: `Item ${index + 1} · ${label}`, field: item[key] }))
  })
  return fields
}

function highlightEvidence(text: string, evidences: string[]): Array<string | { text: string; highlighted: true }> {
  const normalize = (value: string) => {
    let normalized = ''
    const offsets: number[] = []
    let pendingSpace: number | null = null
    for (let index = 0; index < value.length; index += 1) {
      if (/\s/.test(value[index])) {
        if (normalized && pendingSpace === null) pendingSpace = index
        continue
      }
      if (pendingSpace !== null) {
        normalized += ' '
        offsets.push(pendingSpace)
        pendingSpace = null
      }
      const lower = value[index].toLocaleLowerCase('pt-BR')
      normalized += lower
      for (let char = 0; char < lower.length; char += 1) offsets.push(index)
    }
    return { value: normalized, offsets }
  }
  const { value: source, offsets: normalizedToOriginal } = normalize(text)
  const ranges: Array<[number, number]> = []
  for (const evidence of evidences) {
    const needle = normalize(evidence).value
    if (!needle) continue
    let from = 0
    while (from < source.length) {
      const start = source.indexOf(needle, from)
      if (start < 0) break
      ranges.push([start, start + needle.length])
      from = start + needle.length
    }
  }
  const merged = ranges.sort((a, b) => a[0] - b[0]).reduce<Array<[number, number]>>((result, range) => {
    const last = result[result.length - 1]
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1])
    else result.push([...range])
    return result
  }, [])
  if (!merged.length) return [text]
  // Map normalized character positions back to the original page text.
  const output: Array<string | { text: string; highlighted: true }> = []
  let cursor = 0
  merged.forEach(([start, end]) => {
    const originalStart = normalizedToOriginal[start] ?? text.length
    const originalEnd = (normalizedToOriginal[end - 1] ?? originalStart) + 1
    if (originalStart > cursor) output.push(text.slice(cursor, originalStart))
    output.push({ text: text.slice(originalStart, originalEnd), highlighted: true })
    cursor = originalEnd
  })
  if (cursor < text.length) output.push(text.slice(cursor))
  return output
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [loginError, setLoginError] = useState('')
  const [loginBusy, setLoginBusy] = useState(false)

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setLoginError('')
    setLoginBusy(true)
    try {
      const token = await new DocuFlowApi().login(String(form.get('email')), String(form.get('password')))
      const profile = await new DocuFlowApi().me(token.access_token)
      if (!profile.workspaces.length) {
        setLoginError('Sua conta ainda não está associada a um workspace.')
        return
      }
      // O token existe apenas na memória desta sessão do app. Ele nunca é salvo no storage do navegador.
      setSession({ token: token.access_token, profile })
    } catch (error) {
      setLoginError(messageFrom(error))
    } finally {
      setLoginBusy(false)
    }
  }

  if (!session) {
    return <LoginView error={loginError} busy={loginBusy} onSubmit={handleLogin} />
  }

  return <WorkspaceApp key={session.profile.id} session={session} onLogout={() => setSession(null)} />
}

function LoginView({ error, busy, onSubmit }: {
  error: string
  busy: boolean
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
}) {
  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="brand-mark" aria-hidden="true">D</div>
        <p className="eyebrow">DOCUFLOW · CONFERÊNCIA DOCUMENTAL</p>
        <h1 id="login-title">Entre para revisar com clareza.</h1>
        <p className="muted login-intro">Acompanhe documentos, evidências e decisões em um só lugar.</p>
        <form className="login-form" onSubmit={onSubmit}>
          <label htmlFor="login-email">E-mail</label>
          <input id="login-email" name="email" type="email" autoComplete="username" required />
          <label htmlFor="login-password">Senha</label>
          <input id="login-password" name="password" type="password" autoComplete="current-password" required />
          {error && <p className="alert error" role="alert">{error}</p>}
          <button className="button primary full" type="submit" disabled={busy}>
            {busy ? 'Entrando…' : 'Entrar'}
          </button>
        </form>
        <p className="login-footnote">Acesso protegido por workspace e papel de usuário.</p>
      </section>
      <aside className="login-aside" aria-hidden="true">
        <div className="aside-copy">
          <span className="aside-kicker">Do recebimento à revisão</span>
          <h2>Menos conferência manual.<br /><em>Mais confiança</em> na decisão.</h2>
          <p>Extração assistida, validações comerciais e aprovação humana com histórico completo.</p>
        </div>
        <div className="floating-sheet sheet-back"><span>PC-2026-0001</span><i /></div>
        <div className="floating-sheet sheet-front">
          <span className="sheet-label">NOTA FISCAL</span>
          <strong>NF 1042</strong>
          <div className="sheet-rule" /><div className="sheet-rule short" />
          <div className="sheet-stamp">✓ Validado</div>
        </div>
      </aside>
    </main>
  )
}

function WorkspaceApp({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const [workspaceId, setWorkspaceId] = useState(session.profile.workspaces[0]?.id ?? 0)
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [listBusy, setListBusy] = useState(true)
  const [listError, setListError] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [supplierFilter, setSupplierFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [assignmentFilter, setAssignmentFilter] = useState<'all' | 'mine' | 'unassigned'>('all')
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [notice, setNotice] = useState('')
  const [uploadBusy, setUploadBusy] = useState(false)
  const [importMode, setImportMode] = useState(false)
  const [dashboardMode, setDashboardMode] = useState(false)
  const [policyMode, setPolicyMode] = useState(false)
  const activeWorkspaceId = useRef(session.profile.workspaces[0]?.id ?? 0)
  const workspace = session.profile.workspaces.find((item) => item.id === workspaceId) ?? session.profile.workspaces[0]
  const api = useMemo(() => new DocuFlowApi(session.token), [session.token])
  const roles = workspace?.roles ?? []
  const canOperate = roles.includes('operator')
  const canApprove = roles.includes('approver')

  const refreshDocuments = useCallback(async () => {
    if (!workspace) return
    const requestedWorkspaceId = workspace.id
    setListError('')
    try {
      const result = await api.listDocuments(requestedWorkspaceId, assignmentFilter)
      if (activeWorkspaceId.current === requestedWorkspaceId) setDocuments(result)
    } catch (error) {
      if (activeWorkspaceId.current === requestedWorkspaceId) setListError(messageFrom(error))
    } finally {
      if (activeWorkspaceId.current === requestedWorkspaceId) setListBusy(false)
    }
  }, [api, assignmentFilter, workspace])

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshDocuments(), 0)
    return () => window.clearTimeout(timer)
  }, [refreshDocuments])

  const hasProcessing = documents.some((document) => isPending(document.processing_status))
  useEffect(() => {
    if (!hasProcessing) return
    const timer = window.setInterval(() => void refreshDocuments(), 3500)
    return () => window.clearInterval(timer)
  }, [hasProcessing, refreshDocuments])

  const visibleDocuments = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase('pt-BR')
    return documents.filter((document) => {
      const searchable = [
        document.original_filename,
        document.purchase_order_reference ?? '',
        document.supplier_name ?? '',
        String(document.id),
      ].join(' ').toLocaleLowerCase('pt-BR')
      if (normalizedQuery && !searchable.includes(normalizedQuery)) return false
      if (supplierFilter && document.supplier_name !== supplierFilter) return false
      if (statusFilter === 'processing' && !isPending(document.processing_status)) return false
      if (statusFilter === 'review' && (document.processing_status !== 'ready_for_review' || document.review_status !== 'pending')) return false
      if (statusFilter === 'failed' && document.processing_status !== 'failed') return false
      if (statusFilter === 'approved' && document.review_status !== 'approved') return false
      if (fromDate && document.uploaded_at.slice(0, 10) < fromDate) return false
      if (toDate && document.uploaded_at.slice(0, 10) > toDate) return false
      return true
    })
  }, [documents, fromDate, query, statusFilter, supplierFilter, toDate])

  const suppliers = [...new Set(documents.map((document) => document.supplier_name).filter((name): name is string => Boolean(name)))].sort()

  async function handleUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    if (!workspace || !canOperate) return
    const form = new window.FormData(formElement)
    const fileInput = formElement.elements.namedItem('file') as HTMLInputElement | null
    const uploadFile = fileInput?.files?.[0]
    const reference = String(form.get('purchase_order_reference') ?? '').trim()
    if (!uploadFile?.name || !uploadFile.size || !reference) {
      setNotice('Selecione um arquivo e informe a referência do pedido.')
      return
    }
    if (!['application/pdf', 'image/png', 'image/jpeg'].includes(uploadFile.type)) {
      setNotice('Envie um arquivo PDF, PNG ou JPEG.')
      return
    }
    setUploadBusy(true)
    setNotice('')
    try {
      const result = await api.upload(workspace.id, uploadFile, reference)
      setNotice(result.duplicate
        ? `Este arquivo já existe. Documento #${result.document_id}.`
        : `Documento #${result.document_id} recebido e colocado na fila.`)
      formElement.reset()
      setQuery('')
      setSupplierFilter('')
      setFromDate('')
      setToDate('')
      await refreshDocuments()
      setStatusFilter('all')
    } catch (error) {
      setNotice(messageFrom(error))
      await refreshDocuments()
    } finally {
      setUploadBusy(false)
    }
  }

  if (selectedId !== null) {
    return (
      <DocumentView
        key={selectedId}
        api={api}
        workspace={workspace}
        documentId={selectedId}
        canOperate={canOperate}
        canApprove={canApprove}
        onBack={() => { setSelectedId(null); void refreshDocuments() }}
      />
    )
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#home" onClick={(event) => event.preventDefault()} aria-label="DocuFlow início">
          <span className="brand-mark small" aria-hidden="true">D</span><span>DocuFlow</span>
        </a>
        <div className="workspace-select-wrap">
          <label htmlFor="workspace-select">Workspace</label>
          <select id="workspace-select" value={workspace?.id ?? ''} onChange={(event) => {
            const nextWorkspaceId = Number(event.target.value)
            activeWorkspaceId.current = nextWorkspaceId
            setWorkspaceId(nextWorkspaceId)
            setSelectedId(null)
            setDocuments([])
            setListBusy(true)
          }}>
            {session.profile.workspaces.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </div>
        <div className="sidebar-nav" aria-label="Navegação principal">
          <button className={`nav-item nav-button ${dashboardMode ? 'active' : ''}`} type="button" onClick={() => { setDashboardMode(true); setImportMode(false); setPolicyMode(false) }}><span aria-hidden="true">▥</span> Painel operacional</button>
          <button className={`nav-item nav-button ${!dashboardMode && !importMode && !policyMode ? 'active' : ''}`} type="button" onClick={() => { setDashboardMode(false); setImportMode(false); setPolicyMode(false) }}><span aria-hidden="true">▤</span> Documentos</button>
          {canOperate && <button className={`nav-item nav-button ${importMode ? 'active' : ''}`} type="button" onClick={() => { setDashboardMode(false); setImportMode(true); setPolicyMode(false) }}><span aria-hidden="true">⇧</span> Importar pedidos</button>}
          {canOperate && <button className={`nav-item nav-button ${policyMode ? 'active' : ''}`} type="button" onClick={() => { setDashboardMode(false); setImportMode(false); setPolicyMode(true) }}><span aria-hidden="true">⚙</span> Regras comerciais</button>}
          <span className="nav-item muted-nav"><span aria-hidden="true">◷</span> Histórico de revisão</span>
        </div>
        <div className="sidebar-bottom">
          <div className="user-chip"><span className="avatar">{session.profile.email.slice(0, 1).toUpperCase()}</span><span><strong>{session.profile.email}</strong><small>{roles.map(roleLabel).join(' · ')}</small></span></div>
          <button className="button ghost signout" type="button" onClick={onLogout}>Sair da conta</button>
        </div>
      </aside>

      <section className="main-area">
        <header className="topbar">
          <div><span className="breadcrumb">Workspace / {workspace?.name}</span><h1>{dashboardMode ? 'Painel operacional' : importMode ? 'Importar pedidos' : policyMode ? 'Regras comerciais' : 'Documentos'}</h1></div>
          <div className="topbar-meta"><span className="secure-label"><span aria-hidden="true">●</span> Ambiente seguro</span><span className="avatar top-avatar">{session.profile.email.slice(0, 1).toUpperCase()}</span></div>
        </header>

        <div className="content-wrap">
          {policyMode && workspace && canOperate ? (
            <CommercialPolicyView key={workspace.id} api={api} workspaceId={workspace.id} />
          ) : dashboardMode && workspace ? (
            <DashboardView key={workspace.id} api={api} workspaceId={workspace.id} />
          ) : importMode && workspace && canOperate ? (
            <PurchaseOrderImportView
              api={api}
              workspaceId={workspace.id}
              onBack={() => setImportMode(false)}
            />
          ) : <>
          {notice && <div className="alert info" role="status">{notice}</div>}
          {canOperate ? <UploadPanel busy={uploadBusy} onSubmit={handleUpload} /> : (
            <div className="role-note"><span aria-hidden="true">◈</span><span>Seu papel {roles.map(roleLabel).join(' e ')} permite consultar documentos e aprovar quando necessário. O envio de documentos exige o papel operador.</span></div>
          )}

          <section className="document-section" aria-labelledby="documents-heading">
            <div className="section-heading">
              <div><p className="eyebrow">ACOMPANHAMENTO</p><h2 id="documents-heading">Fila de documentos <span className="count-pill">{visibleDocuments.length}</span></h2></div>
              <button className="button secondary" type="button" onClick={() => void refreshDocuments()} disabled={listBusy}>↻ Atualizar</button>
            </div>

            <div className="filter-bar">
              <label className="search-field"><span aria-hidden="true">⌕</span><span className="sr-only">Buscar documentos</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar arquivo, fornecedor ou pedido" /></label>
              <label><span className="sr-only">Filtrar por fornecedor</span><select aria-label="Filtrar por fornecedor" value={supplierFilter} onChange={(event) => setSupplierFilter(event.target.value)}><option value="">Todos os fornecedores</option>{suppliers.map((name) => <option key={name}>{name}</option>)}</select></label>
              <label><span className="sr-only">Filtrar por estado</span><select aria-label="Filtrar por estado" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">Todos os estados</option><option value="processing">Em processamento</option><option value="review">Revisão necessária</option><option value="failed">Falhas</option><option value="approved">Aprovados</option></select></label>
              <label><span className="sr-only">Filtrar por responsável</span><select aria-label="Filtrar por responsável" value={assignmentFilter} onChange={(event) => { setListBusy(true); setAssignmentFilter(event.target.value as typeof assignmentFilter) }}><option value="all">Todos os responsáveis</option><option value="mine">Atribuídos a mim</option><option value="unassigned">Sem responsável</option></select></label>
              <label className="date-filter"><span className="sr-only">Data inicial</span><input aria-label="Data inicial" type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} /></label>
              <label className="date-filter"><span className="sr-only">Data final</span><input aria-label="Data final" type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} /></label>
            </div>

            {listError && <div className="alert error" role="alert">{listError}</div>}
            {listBusy ? <div className="loading-state" role="status">Carregando documentos…</div> : visibleDocuments.length ? (
              <div className="document-table-wrap"><table className="document-table">
                <thead><tr><th>Documento</th><th>Fornecedor / pedido</th><th>Responsável</th><th>Enviado em</th><th>Processamento</th><th>Revisão</th><th><span className="sr-only">Abrir</span></th></tr></thead>
                <tbody>{visibleDocuments.map((document) => <tr key={document.id}>
                  <td><button className="document-link" type="button" onClick={() => setSelectedId(document.id)}><span className="file-icon" aria-hidden="true">▧</span><span><strong>{document.original_filename}</strong><small>#{document.id} · {fileSize(document.size_bytes)}</small></span></button></td>
                  <td><span className="table-primary">{document.supplier_name || 'Fornecedor pendente'}</span><small className="table-secondary">{document.purchase_order_reference || `Pedido #${document.purchase_order_id}`}</small></td>
                  <td><span className="table-primary">{document.assignee_email || 'Sem responsável'}</span><small className="table-secondary">{document.assignee_user_id ? 'Atribuído' : 'Não atribuído'}</small></td>
                  <td className="date-cell">{formatDate(document.uploaded_at)}</td>
                  <td><StatusPill kind={document.processing_status === 'failed' ? 'danger' : isPending(document.processing_status) ? 'progress' : 'neutral'}>{processingLabels[document.processing_status]}</StatusPill>{document.processing_error && <small className="failure-hint">{document.processing_error}</small>}</td>
                  <td><StatusPill kind={document.review_status === 'approved' ? 'success' : document.review_status === 'rejected' || document.processing_status === 'failed' ? 'danger' : document.processing_status === 'ready_for_review' ? 'review' : 'neutral'}>{reviewLabelFor(document)}</StatusPill></td>
                  <td><button className="icon-button" type="button" aria-label={`Abrir ${document.original_filename}`} onClick={() => setSelectedId(document.id)}>→</button></td>
                </tr>)}</tbody>
              </table></div>
            ) : <div className="empty-state"><div className="empty-icon" aria-hidden="true">▤</div><h3>Nenhum documento encontrado</h3><p>Altere os filtros ou envie um documento para começar.</p></div>}
          </section>
          <footer className="footer-note">A extração auxilia a conferência. A decisão final sempre depende de revisão humana.</footer>
          </>}
        </div>
      </section>
    </main>
  )
}

function localDateInput(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function initialDashboardDates(): { startDate: string; endDate: string } {
  const today = new Date()
  const endDate = localDateInput(today)
  const firstDay = new Date(today)
  firstDay.setDate(firstDay.getDate() - 13)
  return { startDate: localDateInput(firstDay), endDate }
}

function CommercialPolicyView({ api, workspaceId }: { api: DocuFlowApi; workspaceId: number }) {
  const [policy, setPolicy] = useState<CommercialPolicy | null>(null)
  const [history, setHistory] = useState<CommercialPolicy[]>([])
  const [rows, setRows] = useState<Array<{ currency: string; tolerance: string }>>([])
  const [busy, setBusy] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    let active = true
    Promise.all([api.getCommercialPolicy(workspaceId), api.getCommercialPolicyHistory(workspaceId)])
      .then(([current, versions]) => {
        if (!active) return
        setPolicy(current)
        setRows(Object.entries(current.currency_tolerances).map(([currency, tolerance]) => ({ currency, tolerance })))
        setHistory(versions)
      })
      .catch((cause: unknown) => { if (active) setError(messageFrom(cause)) })
      .finally(() => { if (active) setBusy(false) })
    return () => { active = false }
  }, [api, retry, workspaceId])

  async function savePolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!policy) return
    const values: Record<string, string> = {}
    for (const row of rows) {
      const currency = row.currency.trim().toUpperCase()
      const tolerance = row.tolerance.trim()
      if (!currency && !tolerance) continue
      if (!/^[A-Z]{3}$/.test(currency) || !/^\d+(?:\.\d{1,2})?$/.test(tolerance) || Number(tolerance) > 100 || currency in values) {
        setError('Informe códigos de moeda únicos com três letras e tolerâncias entre 0 e 100,00, com até duas casas decimais.')
        return
      }
      values[currency] = tolerance
    }
    setSaving(true)
    setError('')
    setNotice('')
    try {
      await api.updateCommercialPolicy(workspaceId, policy.version, values)
      setNotice('Nova versão das regras comerciais salva.')
      setBusy(true)
      setRetry((value) => value + 1)
    } catch (cause) {
      setError(messageFrom(cause))
    } finally {
      setSaving(false)
    }
  }

  return <section className="policy-view" aria-labelledby="policy-title">
    <div className="policy-intro"><div><p className="eyebrow">CONFIGURAÇÃO DO WORKSPACE</p><h2 id="policy-title">Tolerâncias de totais</h2><p>Defina a tolerância usada nas comparações de totais por moeda. Regras de evidência e permissões permanecem iguais.</p></div>
      {policy && <div className="policy-version"><span>Versão atual</span><strong>v{policy.version}</strong></div>}
    </div>
    <p className="policy-limits">Moedas sem regra própria usam {policy?.default_total_tolerance ?? '0.01'}. Até 20 moedas; código de três letras; tolerância de 0 a 100,00 com no máximo duas casas decimais.</p>
    {notice && <div className="alert info" role="status">{notice}</div>}
    {error && <div className="alert error" role="alert">{error}{error.includes('mudou') && <button className="button secondary" type="button" onClick={() => { setBusy(true); setError(''); setRetry((value) => value + 1) }}>Recarregar política</button>}</div>}
    {busy ? <div className="loading-state" role="status">Carregando política…</div> : policy && <>
      <form className="panel policy-form" onSubmit={(event) => void savePolicy(event)}>
        <div className="panel-heading"><div><p className="eyebrow">COMPARAÇÃO COMERCIAL</p><h3>Regras por moeda</h3></div></div>
        {rows.map((row, index) => <div className="policy-rule-row" key={index}>
          <label>Código da moeda<input aria-label={`Moeda ${index + 1}`} maxLength={3} autoCapitalize="characters" value={row.currency} onChange={(event) => setRows((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, currency: event.target.value.toUpperCase() } : item))} placeholder="BRL" /></label>
          <label>Tolerância<input aria-label={`Tolerância ${index + 1}`} inputMode="decimal" value={row.tolerance} onChange={(event) => setRows((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, tolerance: event.target.value } : item))} placeholder="0.01" /></label>
          <button className="button ghost" type="button" aria-label={`Remover regra ${index + 1}`} onClick={() => setRows((items) => items.filter((_, itemIndex) => itemIndex !== index))}>Remover</button>
        </div>)}
        <div className="policy-form-actions">
          <button className="button secondary" type="button" onClick={() => setRows((items) => items.length < 20 ? [...items, { currency: '', tolerance: '' }] : items)} disabled={rows.length >= 20}>＋ Adicionar moeda</button>
          <button className="button primary" type="submit" disabled={saving}>{saving ? 'Salvando…' : `Salvar nova versão (v${policy.version + 1})`}</button>
        </div>
      </form>
      <section className="panel policy-history" aria-labelledby="policy-history-title"><div className="panel-heading"><div><p className="eyebrow">REGISTRO IMUTÁVEL</p><h3 id="policy-history-title">Histórico de alterações</h3></div></div>
        {history.length ? <ol>{history.map((item) => <li key={item.version}><strong>Versão {item.version}</strong><span>{item.created_by_email || 'Membro'} · {item.created_at ? formatDate(item.created_at) : 'Data indisponível'}</span><small>{Object.keys(item.currency_tolerances).length ? Object.entries(item.currency_tolerances).map(([currency, amount]) => `${currency}: ${amount}`).join(' · ') : 'Sem exceções por moeda; usa o padrão.'}</small></li>)}</ol> : <p className="muted">Ainda não há alterações. A versão 0 usa a tolerância padrão.</p>}
      </section>
    </>}
  </section>
}

function DashboardView({ api, workspaceId }: { api: DocuFlowApi; workspaceId: number }) {
  const [initialDates] = useState(initialDashboardDates)
  const [startDate, setStartDate] = useState(initialDates.startDate)
  const [endDate, setEndDate] = useState(initialDates.endDate)
  const [dashboard, setDashboard] = useState<WorkspaceDashboard | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    let active = true
    api.getDashboard(workspaceId, startDate, endDate)
      .then((result) => { if (active) { setDashboard(result); setError('') } })
      .catch((cause: unknown) => { if (active) { setDashboard(null); setError(messageFrom(cause)) } })
      .finally(() => { if (active) setBusy(false) })
    return () => { active = false }
  }, [api, endDate, retry, startDate, workspaceId])

  const documentCards = dashboard ? [
    { label: 'Aguardando processamento', value: dashboard.documents.queued + dashboard.documents.processing, detail: `${dashboard.documents.queued} na fila · ${dashboard.documents.processing} em execução`, tone: 'blue' },
    { label: 'Aguardando revisão', value: dashboard.documents.awaiting_review, detail: 'Prontos sem correção salva', tone: 'amber' },
    { label: 'Aguardando aprovação', value: dashboard.documents.awaiting_approval, detail: 'Com revisão salva, ainda pendentes', tone: 'green' },
    { label: 'Falhas de processamento', value: dashboard.documents.processing_failed, detail: 'Estado atual', tone: 'red' },
  ] : []
  const erpCards = dashboard ? [
    { label: 'Envios pendentes', value: dashboard.erp_submissions.pending, tone: 'amber' },
    { label: 'Envios concluídos', value: dashboard.erp_submissions.succeeded, tone: 'green' },
    { label: 'Envios com falha', value: dashboard.erp_submissions.failed, tone: 'red' },
  ] : []
  const peak = Math.max(1, ...(dashboard?.document_volume.map((point) => point.count) ?? []))

  return <section className="dashboard-view" aria-labelledby="dashboard-title">
    <div className="dashboard-toolbar">
      <div><p className="eyebrow">VISÃO OPERACIONAL</p><h2 id="dashboard-title">Acompanhe a fila e a atividade</h2></div>
      <div className="dashboard-period" aria-label="Período do painel">
        <label>De <input aria-label="Início do período do painel" type="date" value={startDate} max={endDate} onChange={(event) => { setBusy(true); setError(''); setDashboard(null); setStartDate(event.target.value) }} /></label>
        <label>Até <input aria-label="Fim do período do painel" type="date" value={endDate} min={startDate} onChange={(event) => { setBusy(true); setError(''); setDashboard(null); setEndDate(event.target.value) }} /></label>
      </div>
    </div>
    <p className="dashboard-note">Indicadores operacionais para acompanhamento. Não representam métricas financeiras certificadas.</p>
    {error && <div className="alert error" role="alert">{error} <button className="button secondary" type="button" onClick={() => { setBusy(true); setError(''); setRetry((value) => value + 1) }}>Tentar novamente</button></div>}
    {busy ? <div className="loading-state" role="status">Carregando indicadores…</div> : dashboard && <>
      <section className="dashboard-group" aria-labelledby="document-indicators-heading">
        <h3 id="document-indicators-heading">Fila atual de documentos</h3>
        <div className="metric-grid">{documentCards.map((card) => <article className={`metric-card ${card.tone}`} key={card.label}>
          <span>{card.label}</span><strong>{card.value}</strong><small>{card.detail}</small>
        </article>)}</div>
      </section>
      <section className="dashboard-group" aria-labelledby="erp-indicators-heading">
        <h3 id="erp-indicators-heading">Envios ao ERP no período</h3>
        <div className="metric-grid erp-metric-grid">{erpCards.map((card) => <article className={`metric-card ${card.tone}`} key={card.label}>
          <span>{card.label}</span><strong>{card.value}</strong>
        </article>)}</div>
      </section>
      <div className="dashboard-charts">
        <section className="dashboard-panel" aria-labelledby="volume-heading">
          <div className="dashboard-panel-heading"><div><p className="eyebrow">DOCUMENTOS RECEBIDOS</p><h3 id="volume-heading">Volume no período</h3></div><strong>{dashboard.volume_total}</strong></div>
          {dashboard.document_volume.length ? <div className="volume-chart" role="group" aria-label={`Volume de documentos por dia no período, ${dashboard.volume_total} documentos`}>
            {dashboard.document_volume.map((point) => <div className="volume-row" key={point.date}>
              <time dateTime={point.date}>{new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: 'short', timeZone: 'UTC' }).format(new Date(`${point.date}T12:00:00Z`))}</time>
              <div className="volume-track"><span style={{ width: `${Math.max(4, (point.count / peak) * 100)}%` }} /></div>
              <strong>{point.count}</strong>
            </div>)}
          </div> : <p className="dashboard-empty">Nenhum documento recebido neste período.</p>}
        </section>
        <section className="dashboard-panel" aria-labelledby="divergences-heading">
          <div className="dashboard-panel-heading"><div><p className="eyebrow">CONFERÊNCIA COMERCIAL</p><h3 id="divergences-heading">Divergências mais comuns</h3></div></div>
          {dashboard.common_divergences.length ? <ol className="divergence-list">{dashboard.common_divergences.map((finding) => <li key={finding.code}><span>{finding.code.replaceAll('_', ' ')}</span><strong>{finding.count}</strong></li>)}</ol> : <p className="dashboard-empty">Nenhuma divergência registrada neste período.</p>}
        </section>
      </div>
      {!dashboard.volume_total && <p className="dashboard-empty period-empty">Sem atividade no período selecionado. A fila atual continua exibida acima.</p>}
    </>}
  </section>
}

function PurchaseOrderImportView({ api, workspaceId, onBack }: {
  api: DocuFlowApi
  workspaceId: number
  onBack: () => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<PurchaseOrderImportPreview | null>(null)
  const [confirmation, setConfirmation] = useState<PurchaseOrderImportConfirmation | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function createPreview() {
    if (!file) {
      setError('Selecione um arquivo CSV para gerar a prévia.')
      return
    }
    setBusy(true)
    setError('')
    setConfirmation(null)
    try {
      setPreview(await api.previewPurchaseOrderImport(workspaceId, file))
    } catch (cause) {
      setError(messageFrom(cause))
      setPreview(null)
    } finally {
      setBusy(false)
    }
  }

  async function confirmImport() {
    if (!file || !preview || preview.orders.some((order) => order.outcome === 'conflict')) return
    setBusy(true)
    setError('')
    try {
      setConfirmation(await api.confirmPurchaseOrderImport(workspaceId, file))
    } catch (cause) {
      setError(messageFrom(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="po-import" aria-labelledby="po-import-title">
      <div className="section-heading">
        <div><p className="eyebrow">CADASTRO EM LOTE</p><h2 id="po-import-title">Importar pedidos por CSV</h2></div>
        <button className="button secondary" type="button" onClick={onBack}>Voltar aos documentos</button>
      </div>
      <p className="po-import-copy">Use UTF-8 com vírgula ou ponto e vírgula. Cada linha representa um item; linhas com a mesma referência formam um pedido. O fornecedor precisa estar cadastrado neste workspace.</p>
      <div className="po-import-controls">
        <label htmlFor="purchase-order-csv">Arquivo CSV</label>
        <input id="purchase-order-csv" type="file" accept=".csv,text/csv" onChange={(event) => {
          setFile(event.currentTarget.files?.[0] ?? null)
          setPreview(null)
          setConfirmation(null)
          setError('')
        }} />
        <button className="button primary" type="button" disabled={busy || !file} onClick={() => void createPreview()}>{busy ? 'Aguarde…' : 'Gerar prévia'}</button>
      </div>
      {error && <div className="alert error" role="alert">{error}</div>}
      {preview && <>
        <div className="po-import-summary" role="status">{preview.valid_rows} linhas válidas de {preview.total_rows} · {preview.orders.length} pedidos identificados</div>
        {preview.orders.length > 0 && <div className="document-table-wrap"><table className="document-table">
          <thead><tr><th>Pedido</th><th>Fornecedor</th><th>Itens</th><th>Total</th><th>Linhas CSV</th><th>Resultado</th></tr></thead>
          <tbody>{preview.orders.map((order) => <tr key={order.reference}>
            <td>{order.reference}</td><td>{order.supplier_name}</td><td>{order.item_count}</td><td>{order.currency} {order.total}</td>
            <td>{order.line_numbers.join(', ')}</td>
            <td><StatusPill kind={order.outcome === 'new' ? 'success' : order.outcome === 'conflict' ? 'danger' : 'neutral'}>{order.outcome === 'new' ? 'Será importado' : order.outcome === 'duplicate' ? 'Duplicata idêntica' : 'Conflito'}</StatusPill></td>
          </tr>)}</tbody>
        </table></div>}
        {preview.errors.length > 0 && <section className="po-import-errors" aria-labelledby="po-import-errors-title">
          <h3 id="po-import-errors-title">Erros encontrados</h3>
          <ul>{preview.errors.map((item, index) => <li key={`${item.line_number}-${index}`}><strong>Linha {item.line_number}:</strong> {item.reason}</li>)}</ul>
        </section>}
        <button className="button primary" type="button" disabled={busy || preview.valid_rows === 0 || preview.orders.some((order) => order.outcome === 'conflict')} onClick={() => void confirmImport()}>{busy ? 'Importando…' : 'Confirmar importação'}</button>
        {preview.orders.some((order) => order.outcome === 'conflict') && <p className="blocked-copy">Resolva os pedidos com conflito antes de confirmar o arquivo.</p>}
      </>}
      {confirmation && <div className="alert info" role="status">
        Importação concluída: {confirmation.imported.length} pedidos novos e {confirmation.ignored_duplicates.length} duplicatas ignoradas.
      </div>}
    </section>
  )
}

function UploadPanel({ busy, onSubmit }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return (
    <section className="upload-panel" aria-labelledby="upload-title">
      <div className="upload-copy"><div className="upload-symbol" aria-hidden="true">↑</div><div><p className="eyebrow">NOVA CONFERÊNCIA</p><h2 id="upload-title">Envie um documento</h2><p>PDF ou imagem · até 10 MB · até 5 páginas por PDF</p></div></div>
      <form className="upload-form" onSubmit={onSubmit}>
        <label htmlFor="purchase-order-reference">Referência do pedido</label>
        <input id="purchase-order-reference" name="purchase_order_reference" placeholder="PC-2026-0001" required />
        <label className="file-input-label" htmlFor="document-file">Arquivo PDF, PNG ou JPEG</label>
        <input id="document-file" className="file-input" name="file" type="file" accept="application/pdf,image/png,image/jpeg" />
        <button className="button primary" type="submit" disabled={busy}>{busy ? 'Enviando…' : 'Enviar para análise'}</button>
      </form>
    </section>
  )
}

function DocumentView({ api, workspace, documentId, canOperate, canApprove, onBack }: {
  api: DocuFlowApi
  workspace: WorkspaceAccess | undefined
  documentId: number
  canOperate: boolean
  canApprove: boolean
  onBack: () => void
}) {
  const [document, setDocument] = useState<DocumentDetail | null>(null)
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [busy, setBusy] = useState(true)
  const [pageError, setPageError] = useState('')
  const [actionError, setActionError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [draft, setDraft] = useState<ReferenceDocument | null>(null)
  const [editMode, setEditMode] = useState(false)
  const [correctionReason, setCorrectionReason] = useState('')
  const [rejectionReason, setRejectionReason] = useState('')
  const [erpSubmission, setErpSubmission] = useState<ERPSubmission | null>(null)
  const [erpBusy, setErpBusy] = useState(false)
  const [selectedPage, setSelectedPage] = useState(1)
  const [originalUrl, setOriginalUrl] = useState<string | null>(null)
  const [originalType, setOriginalType] = useState('')
  const [originalState, setOriginalState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [originalError, setOriginalError] = useState('')
  const [originalRetry, setOriginalRetry] = useState(0)
  const [workspaceMembers, setWorkspaceMembers] = useState<WorkspaceMember[]>([])
  const [membersError, setMembersError] = useState('')
  const [membersBusy, setMembersBusy] = useState(true)
  const [assignmentTarget, setAssignmentTarget] = useState<number | null | undefined>(undefined)
  const [assignmentBusy, setAssignmentBusy] = useState(false)

  const refresh = useCallback(async () => {
    if (!workspace) return
    setPageError('')
    try {
      const [details, events] = await Promise.all([
        api.getDocument(workspace.id, documentId),
        api.getAudit(workspace.id, documentId),
      ])
      setDocument(details)
      setAudit(events)
      setDraft(details.reviewed_document ?? details.extracted_document)
      setConflict(false)
      const latestSubmissionId = [...events].reverse().find((event) =>
        ['erp_submission', 'erp_attempt', 'erp_success', 'erp_failure'].includes(event.event_type)
        && typeof event.details?.submission_id === 'number',
      )?.details?.submission_id
      if (canApprove && typeof latestSubmissionId === 'number') {
        try {
          setErpSubmission(await api.getERPSubmission(workspace.id, latestSubmissionId))
        } catch {
          setErpSubmission(null)
        }
      } else {
        setErpSubmission(null)
      }
      return details
    } catch (error) {
      setPageError(messageFrom(error))
    } finally {
      setBusy(false)
    }
  }, [api, canApprove, documentId, workspace])

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0)
    return () => window.clearTimeout(timer)
  }, [refresh])

  useEffect(() => {
    if (!workspace || !canOperate) return
    let active = true
    void api.listWorkspaceMembers(workspace.id).then((members) => {
      if (active) setWorkspaceMembers(members)
    }).catch((error: unknown) => {
      if (active) setMembersError(messageFrom(error))
    }).finally(() => {
      if (active) setMembersBusy(false)
    })
    return () => { active = false }
  }, [api, canOperate, workspace])

  useEffect(() => {
    let active = true
    let createdUrl: string | null = null
    if (!workspace) {
      void Promise.resolve().then(() => {
        if (!active) return
        setOriginalError('Selecione um workspace para carregar o arquivo.')
        setOriginalState('error')
      })
      return
    }
    void api.download(workspace.id, documentId, true).then(async (response) => {
      if (!response.ok) throw new Error('Não foi possível carregar o arquivo.')
      const blob = await response.blob()
      createdUrl = URL.createObjectURL(blob)
      if (active) {
        setOriginalType(response.headers.get('content-type')?.split(';')[0] || blob.type)
        setOriginalUrl(createdUrl)
        setOriginalState('ready')
      }
    }).catch((error: unknown) => {
      if (!active) return
      setOriginalError(messageFrom(error))
      setOriginalState('error')
    })
    return () => {
      active = false
      if (createdUrl) URL.revokeObjectURL(createdUrl)
    }
  }, [api, documentId, originalRetry, workspace])

  useEffect(() => {
    if (!document || !isPending(document.processing_status)) return
    const timer = window.setInterval(() => void refresh(), 3500)
    return () => window.clearInterval(timer)
  }, [document, refresh])

  useEffect(() => {
    if (!workspace || !erpSubmission || !['queued', 'processing'].includes(erpSubmission.state)) return
    const timer = window.setInterval(async () => {
      try {
        const updated = await api.getERPSubmission(workspace.id, erpSubmission.id)
        setErpSubmission(updated)
      } catch (error) {
        setActionError(messageFrom(error))
      }
    }, 2500)
    return () => window.clearInterval(timer)
  }, [api, erpSubmission, workspace])

  async function submitCorrection() {
    if (!workspace || !document || !draft) return
    if (!correctionReason.trim()) {
      setActionError('Informe uma justificativa para a correção.')
      return
    }
    setActionError('')
    try {
      await api.correct(workspace.id, documentId, {
        expected_version: document.review_version,
        justification: correctionReason.trim(),
        reviewed_document: draft,
      })
      setEditMode(false)
      setCorrectionReason('')
      await refresh()
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setConflict(true)
        setActionError('O documento mudou desde que você o abriu. Recarregue para ver a versão atual antes de salvar.')
      } else setActionError(messageFrom(error))
    }
  }

  async function approve() {
    if (!workspace || !document) return
    setActionError('')
    try {
      await api.approve(workspace.id, documentId, document.review_version)
      await refresh()
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setConflict(true)
        setActionError('O documento mudou desde que você o abriu. Recarregue antes de aprovar.')
      } else setActionError(messageFrom(error))
    }
  }

  async function reject() {
    if (!workspace || !document) return
    if (!rejectionReason.trim()) {
      setActionError('Informe uma justificativa para rejeitar.')
      return
    }
    setActionError('')
    try {
      await api.reject(workspace.id, documentId, document.review_version, rejectionReason.trim())
      setRejectionReason('')
      await refresh()
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setConflict(true)
        setActionError('O documento mudou desde que você o abriu. Recarregue antes de rejeitar.')
      } else setActionError(messageFrom(error))
    }
  }

  async function requestErp() {
    if (!workspace || !document?.approval) return
    setErpBusy(true)
    setActionError('')
    try {
      const submission = await api.requestERP(workspace.id, documentId, document.approval.approved_version)
      setErpSubmission(submission)
    } catch (error) {
      setActionError(messageFrom(error))
    } finally {
      setErpBusy(false)
    }
  }

  async function saveAssignment() {
    if (!workspace || !document) return
    const assigneeUserId = assignmentTarget === undefined ? document.assignee_user_id : assignmentTarget
    setAssignmentBusy(true)
    setActionError('')
    try {
      await api.assignDocument(workspace.id, documentId, assigneeUserId)
      setAssignmentTarget(undefined)
      await refresh()
    } catch (error) {
      setActionError(messageFrom(error))
    } finally {
      setAssignmentBusy(false)
    }
  }

  async function downloadOriginal() {
    if (!workspace) return
    try {
      const response = await api.download(workspace.id, documentId)
      const blobUrl = URL.createObjectURL(await response.blob())
      const link = window.document.createElement('a')
      link.href = blobUrl
      link.download = document?.original_filename || 'documento'
      link.click()
      URL.revokeObjectURL(blobUrl)
    } catch (error) {
      setActionError(messageFrom(error))
    }
  }

  if (busy) return <main className="app-shell detail-shell"><div className="loading-state" role="status">Carregando documento…</div></main>
  if (!document) return <main className="app-shell detail-shell"><button className="button ghost" onClick={onBack}>← Voltar</button><div className="alert error" role="alert">{pageError || 'Documento não encontrado.'}</div></main>

  const currentDocument = draft ?? document.reviewed_document ?? document.extracted_document
  const pageTexts = document.page_texts ?? []
  const pageCount = Math.max(pageTexts.length, 1)
  const selectedPageText = pageTexts.find((page) => page.number === selectedPage) ?? pageTexts[0]
  const evidenceFields = fieldsOf(currentDocument).filter(({ field }) => field.page === selectedPage && field.evidence?.trim())
  const evidenceTexts = evidenceFields.map(({ field }) => field.evidence as string)
  const matchedEvidence = evidenceTexts.some((evidence) => highlightEvidence(selectedPageText?.text ?? '', [evidence]).some((part) => typeof part !== 'string'))
  const approvalIsCurrent = Boolean(document.approval && document.approval.approved_version === document.review_version && document.review_status === 'approved')
  const canDecide = canApprove && document.processing_status === 'ready_for_review' && document.review_status === 'pending'
  const validation = document.validation_result

  return (
    <main className="app-shell detail-shell">
      <section className="main-area detail-area">
        <header className="topbar detail-topbar">
          <div><button className="back-link" type="button" onClick={onBack}>← Voltar para documentos</button><span className="breadcrumb">{workspace?.name} / Documento #{document.id}</span><h1>{document.original_filename}</h1></div>
          <div className="detail-actions"><StatusPill kind={document.processing_status === 'failed' ? 'danger' : isPending(document.processing_status) ? 'progress' : 'neutral'}>{processingLabels[document.processing_status]}</StatusPill><button className="button secondary" type="button" onClick={() => void downloadOriginal()}>↓ Baixar original</button></div>
        </header>

        <div className="detail-content">
          {pageError && <div className="alert error" role="alert">{pageError}</div>}
          {actionError && <div className="alert error" role="alert">{actionError}</div>}
          {conflict && <button className="button secondary reload-button" type="button" onClick={() => { setBusy(true); void refresh() }}>Recarregar documento</button>}

          <div className="detail-summary-card">
            <div><span className="eyebrow">FORNECEDOR</span><strong>{document.supplier_name || currentDocument?.supplier_name.value || 'Não identificado'}</strong></div>
            <div><span className="eyebrow">PEDIDO DE COMPRA</span><strong>{document.purchase_order_reference || currentDocument?.purchase_order_reference.value || `#${document.purchase_order_id}`}</strong></div>
            <div><span className="eyebrow">ENVIADO EM</span><strong>{formatDate(document.uploaded_at)}</strong></div>
            <div><span className="eyebrow">REVISÃO</span><StatusPill kind={document.review_status === 'approved' ? 'success' : document.review_status === 'rejected' || document.processing_status === 'failed' ? 'danger' : document.processing_status === 'ready_for_review' ? 'review' : 'neutral'}>{reviewLabelFor(document)} · v{document.review_version}</StatusPill></div>
            {validation && <div><span className="eyebrow">REGRAS COMERCIAIS</span><strong>Política v{validation.commercial_policy.version}</strong></div>}
          </div>

          {document.processing_status === 'failed' && <div className="alert error"><strong>Falha no processamento.</strong> {document.processing_error || 'Tente processar o documento novamente mais tarde.'}</div>}
          {isPending(document.processing_status) && <div className="processing-banner"><span className="spinner" aria-hidden="true" /><span>{document.processing_status === 'queued' ? 'Documento na fila. Atualizando o andamento automaticamente…' : 'Extraindo e validando o documento…'}</span></div>}

          <div className="detail-grid">
            <div className="detail-main-column">
              <div className="review-comparison">
              <section className="panel original-viewer" aria-labelledby="original-heading">
                <div className="panel-heading"><div><p className="eyebrow">CONFERÊNCIA VISUAL</p><h2 id="original-heading">Arquivo original</h2></div></div>
                <div className="viewer-controls" aria-label="Navegação de páginas">
                  <button className="button secondary" type="button" aria-label="Página anterior" onClick={() => setSelectedPage((page) => Math.max(1, page - 1))} disabled={selectedPage <= 1}>←</button>
                  <span aria-live="polite">Página {selectedPage} de {pageCount}</span>
                  <button className="button secondary" type="button" aria-label="Próxima página" onClick={() => setSelectedPage((page) => Math.min(pageCount, page + 1))} disabled={selectedPage >= pageCount}>→</button>
                </div>
                {pageCount > 1 && <div className="page-jump-list" aria-label="Selecionar página">{pageTexts.map((page) => <button key={page.number} type="button" className={`button page-jump${selectedPage === page.number ? ' active' : ''}`} aria-label={`Ir para página ${page.number}`} aria-current={selectedPage === page.number ? 'page' : undefined} onClick={() => setSelectedPage(page.number)}>{page.number}</button>)}</div>}
                {originalState === 'loading' && <div className="viewer-placeholder" role="status">Carregando arquivo original…</div>}
                {originalState === 'error' && <div className="viewer-placeholder viewer-error"><p role="alert">{originalError}</p><button className="button secondary" type="button" onClick={() => { setOriginalState('loading'); setOriginalError(''); setOriginalUrl(null); setOriginalRetry((attempt) => attempt + 1) }}>Tentar carregar arquivo novamente</button></div>}
                {originalState === 'ready' && originalUrl && (originalType === 'application/pdf' || document.content_type === 'application/pdf'
                  ? <iframe className="original-frame" key={`${originalUrl}-${selectedPage}`} src={`${originalUrl}#page=${selectedPage}`} title={`Documento original, página ${selectedPage}`} />
                  : originalType === 'image/png' || originalType === 'image/jpeg' || document.content_type.startsWith('image/')
                    ? <img className="original-image" src={originalUrl} alt={`Página ${selectedPage} do documento original`} />
                    : <div className="viewer-placeholder" role="status">Este tipo de arquivo não pode ser exibido aqui. Use “Baixar original”.</div>)}
                <div className="page-text-preview" aria-label={`Texto extraído da página ${selectedPage}`} aria-live="polite">
                  <div className="page-text-heading"><strong>Texto da página {selectedPage}</strong>{selectedPageText && <StatusPill kind={selectedPageText.source === 'ocr' ? 'warning' : 'neutral'}>{selectedPageText.source === 'ocr' ? 'OCR' : 'native'}</StatusPill>}</div>
                  {selectedPageText?.text ? <p>{highlightEvidence(selectedPageText.text, evidenceTexts).map((part, index) => typeof part === 'string' ? part : <mark key={index}>{part.text}</mark>)}</p> : <p className="muted">O texto desta página ainda não está disponível.</p>}
                  {evidenceTexts.length > 0 && !matchedEvidence && <p className="evidence-missing" role="status">Evidência não localizada no texto extraído desta página.</p>}
                </div>
              </section>
              <div className="review-data-column">
              <section className="panel" aria-labelledby="extracted-heading">
                <div className="panel-heading"><div><p className="eyebrow">DADOS DO DOCUMENTO</p><h2 id="extracted-heading">Extração e revisão</h2></div>{canOperate && document.processing_status === 'ready_for_review' && document.extracted_document && <button className="button secondary" type="button" onClick={() => setEditMode(!editMode)}>{editMode ? 'Cancelar edição' : 'Editar dados'}</button>}</div>
                {currentDocument ? (
                  <>
                    {editMode && <div className="revision-note">A extração original é preservada. As alterações serão guardadas como uma nova versão revisada.</div>}
                    <div className="field-grid">
                      {fieldLabels.map(([key, label]) => <ExtractedFieldView
                        key={key}
                        label={label}
                        field={currentDocument[key]}
                        editable={editMode}
                        onEvidenceClick={currentDocument[key].page ? () => setSelectedPage(currentDocument[key].page as number) : undefined}
                        source={pageTexts.find((page) => page.number === currentDocument[key].page)?.source}
                        onChange={(updated) => setDraft({ ...currentDocument, [key]: updated })}
                      />)}
                    </div>
                    <section className="items-section"><h3>Itens da nota <span className="count-pill">{currentDocument.items.length}</span></h3>
                      {editMode && <button className="button secondary add-item" type="button" onClick={() => setDraft({ ...currentDocument, items: [...currentDocument.items, emptyItem()] })}>＋ Adicionar item</button>}
                      {currentDocument.items.map((item, index) => <div className="item-card" key={`${item.code.value ?? 'item'}-${index}`}>
                        <div className="item-title"><strong>{index + 1}. {displayValue(item.description.value)}</strong><span>{displayValue(item.code.value)}</span>{editMode && <button className="remove-item" type="button" aria-label={`Remover item ${index + 1}`} onClick={() => setDraft({ ...currentDocument, items: currentDocument.items.filter((_, itemIndex) => itemIndex !== index) })}>Remover</button>}</div>
                        <div className="item-fields">
                          {([['code', 'Código'], ['description', 'Descrição'], ['quantity', 'Quantidade'], ['unit_price', 'Preço unitário'], ['line_total', 'Total do item']] as const).map(([fieldKey, label]) => <ExtractedFieldView
                            key={fieldKey}
                            label={`Item ${index + 1} · ${label}`}
                            field={item[fieldKey]}
                            editable={editMode}
                            compact
                            onEvidenceClick={item[fieldKey].page ? () => setSelectedPage(item[fieldKey].page as number) : undefined}
                            source={pageTexts.find((page) => page.number === item[fieldKey].page)?.source}
                            onChange={(updated) => {
                              const items = [...currentDocument.items]
                              items[index] = { ...item, [fieldKey]: updated }
                              setDraft({ ...currentDocument, items })
                            }}
                          />)}
                        </div>
                      </div>)}
                    </section>
                    {currentDocument.unsupported_elements.length > 0 && <div className="alert warning"><strong>Itens que precisam de atenção</strong><ul>{currentDocument.unsupported_elements.map((element) => <li key={element}>{element}</li>)}</ul></div>}
                    {editMode && <div className="review-form unsupported-editor"><label htmlFor="unsupported-elements">Elementos que exigem atenção · um por linha</label><textarea id="unsupported-elements" value={currentDocument.unsupported_elements.join('\n')} rows={3} onChange={(event) => setDraft({ ...currentDocument, unsupported_elements: event.target.value.split('\n').map((value) => value.trim()).filter(Boolean) })} /></div>}
                    {editMode && <div className="review-form"><label htmlFor="correction-reason">Justificativa da correção</label><textarea id="correction-reason" value={correctionReason} onChange={(event) => setCorrectionReason(event.target.value)} rows={3} required /><button className="button primary" type="button" onClick={() => void submitCorrection()}>Salvar revisão</button></div>}
                  </>
                ) : <div className="empty-inline">Os dados extraídos aparecerão quando o processamento terminar.</div>}
              </section>

              <section className="panel" aria-labelledby="findings-heading">
                <div className="panel-heading"><div><p className="eyebrow">CONFERÊNCIA AUTOMÁTICA</p><h2 id="findings-heading">Achados comerciais</h2></div>{validation && <StatusPill kind={validation.status === 'blocked' ? 'danger' : validation.status === 'divergent' ? 'warning' : 'success'}>{validationLabel(validation.status)}</StatusPill>}</div>
                {validation?.findings.length ? <ul className="finding-list">{validation.findings.map((finding, index) => <li className={`finding ${finding.severity}`} key={`${finding.code}-${index}`}><span aria-hidden="true">{finding.severity === 'blocking_error' ? '!' : finding.severity === 'divergence' ? '↔' : 'i'}</span><div><strong>{findingSeverity(finding.severity)}</strong><p>{finding.message}</p></div></li>)}</ul> : validation ? <div className="success-empty">✓ Nenhuma divergência comercial encontrada.</div> : <div className="empty-inline">A validação será exibida após o processamento.</div>}
              </section>
              </div>
              </div>
            </div>

            <aside className="detail-side-column">
              <section className="panel assignment-panel" aria-labelledby="assignment-heading">
                <p className="eyebrow">ORGANIZAÇÃO DA FILA</p>
                <h2 id="assignment-heading">Responsável</h2>
                <p className={`assignment-owner${document.assignee_user_id ? '' : ' unassigned'}`}>{document.assignee_email || 'Sem responsável'}</p>
                <StatusPill kind={document.assignee_user_id ? 'review' : 'neutral'}>{document.assignee_user_id ? 'Atribuído' : 'Não atribuído'}</StatusPill>
                {document.assigned_by_email && document.assigned_at && <small className="assignment-meta">{document.assignee_user_id ? 'Atribuído' : 'Alterado'} por {document.assigned_by_email} · {formatDate(document.assigned_at)}</small>}
                {canOperate && <div className="assignment-controls">
                  {membersError && <div className="alert error" role="alert">{membersError}</div>}
                  <label htmlFor="document-assignee">Atribuir a membro ativo</label>
                  <select
                    id="document-assignee"
                    aria-label="Responsável"
                    value={assignmentTarget === undefined ? (document.assignee_user_id ?? '') : (assignmentTarget ?? '')}
                    disabled={membersBusy || assignmentBusy || Boolean(membersError)}
                    onChange={(event) => setAssignmentTarget(event.target.value ? Number(event.target.value) : null)}
                  >
                    <option value="">Sem responsável</option>
                    {workspaceMembers.map((member) => <option key={member.id} value={member.id}>{member.email} · {member.roles.map(roleLabel).join(' e ')}</option>)}
                  </select>
                  <button className="button secondary full" type="button" disabled={membersBusy || assignmentBusy || Boolean(membersError)} onClick={() => void saveAssignment()}>{assignmentBusy ? 'Salvando…' : 'Salvar atribuição'}</button>
                </div>}
              </section>
              {canDecide && <section className="panel decision-panel" aria-labelledby="decision-heading"><p className="eyebrow">AÇÃO HUMANA</p><h2 id="decision-heading">Decisão de revisão</h2><p>Confira os campos e achados antes de registrar uma decisão.</p>
                <button className="button primary full" type="button" onClick={() => void approve()} disabled={validation?.status === 'blocked'}>✓ Aprovar documento</button>
                {validation?.status === 'blocked' && <p className="blocked-copy">Há erros bloqueantes. Corrija os dados antes da aprovação.</p>}
                <label htmlFor="rejection-reason">Motivo da rejeição</label><textarea id="rejection-reason" value={rejectionReason} onChange={(event) => setRejectionReason(event.target.value)} rows={3} placeholder="Explique por que o documento foi rejeitado" />
                <button className="button danger full" type="button" onClick={() => void reject()}>Rejeitar documento</button>
              </section>}

              {document.review_status === 'approved' && approvalIsCurrent && canApprove && <section className="panel erp-panel" aria-labelledby="erp-heading"><p className="eyebrow">INTEGRAÇÃO SIMULADA</p><h2 id="erp-heading">ERP de demonstração</h2><p>Envie os dados do snapshot aprovado. Nenhum ERP externo será chamado.</p>
                {erpSubmission ? <div className="erp-status"><StatusPill kind={erpSubmission.state === 'succeeded' ? 'success' : erpSubmission.state === 'failed' ? 'danger' : 'progress'}>{erpLabel(erpSubmission.state)}</StatusPill><p>Versão aprovada: v{erpSubmission.approved_version}</p><p>Tentativas: {erpSubmission.attempts}</p>{erpSubmission.erp_identifier && <strong className="erp-id">{erpSubmission.erp_identifier}</strong>}{erpSubmission.failure_message && <p className="failure-hint">{erpSubmission.failure_message}</p>}{erpSubmission.state === 'failed' && <button className="button secondary full" type="button" disabled={erpBusy} onClick={() => void requestErp()}>Tentar novamente</button>}</div> : <button className="button secondary full" type="button" disabled={erpBusy} onClick={() => void requestErp()}>{erpBusy ? 'Enviando…' : 'Enviar ao ERP de demonstração'}</button>}
              </section>}

              <section className="panel audit-panel" aria-labelledby="audit-heading"><div className="panel-heading"><div><p className="eyebrow">HISTÓRICO IMUTÁVEL</p><h2 id="audit-heading">Auditoria</h2></div></div>{audit.length ? <ol className="audit-list">{audit.map((event) => <li key={event.id}><span className="audit-dot" aria-hidden="true" /><div><strong>{eventLabels[event.event_type]}</strong><small>{formatDate(event.created_at)} · v{event.version}</small>{event.justification && <p>{event.justification}</p>}{event.details && Object.keys(event.details).length > 0 && <small className="audit-details">{Object.entries(event.details).map(([key, value]) => `${key}: ${String(value)}`).join(' · ')}</small>}</div></li>)}</ol> : <div className="empty-inline">Nenhum evento registrado ainda.</div>}</section>
            </aside>
          </div>
          <footer className="footer-note">A revisão salva a versão esperada. Se outra pessoa alterar o documento, atualize a tela antes de continuar.</footer>
        </div>
      </section>
    </main>
  )
}

function ExtractedFieldView({ label, field, editable, compact = false, source, onEvidenceClick, onChange }: {
  label: string
  field: ExtractedField
  editable: boolean
  compact?: boolean
  source?: 'native' | 'ocr'
  onEvidenceClick?: () => void
  onChange: (value: ExtractedField) => void
}) {
  const fieldId = useId()
  return (
    <div className={`extracted-field${compact ? ' compact' : ''}`}>
      {editable
        ? <label htmlFor={fieldId}>{label} <span className="field-page">{field.page ? `· pág. ${field.page}` : ''}</span></label>
        : <span className="field-label">{label} <span className="field-page">{field.page ? `· pág. ${field.page}` : ''}</span></span>}
      {editable ? <>
        <input id={fieldId} aria-label={`${label} valor`} value={field.value ?? ''} onChange={(event) => onChange({ ...field, value: event.target.value || null })} />
        <div className="evidence-edit"><input aria-label={`${label} evidência`} value={field.evidence ?? ''} placeholder="Evidência" onChange={(event) => onChange({ ...field, evidence: event.target.value || null })} /><input aria-label={`${label} página`} type="number" min="1" value={field.page ?? ''} placeholder="Pág." onChange={(event) => onChange({ ...field, page: event.target.value ? Number(event.target.value) : null })} /></div>
      </> : <>
        <strong id={fieldId}>{displayValue(field.value)}</strong>
        {field.evidence && <button className="evidence-quote evidence-link" type="button" onClick={onEvidenceClick} disabled={!onEvidenceClick} aria-label={`Ver evidência de ${label} na página ${field.page ?? 'desconhecida'}`}>“{field.evidence}”</button>}
      </>}
      {source && <small className="field-origin">Origem: {source === 'ocr' ? 'OCR' : 'native'}</small>}
      {field.raw_value && <small className="raw-value">Original extraído: {field.raw_value}</small>}
    </div>
  )
}

function StatusPill({ kind, children }: { kind: 'neutral' | 'progress' | 'danger' | 'success' | 'review' | 'warning'; children: import('react').ReactNode }) {
  return <span className={`status-pill ${kind}`}><span className="status-dot" aria-hidden="true" />{children}</span>
}

function roleLabel(role: string): string {
  return role === 'operator' ? 'operador' : 'aprovador'
}

function validationLabel(status: string): string {
  if (status === 'blocked') return 'Bloqueado'
  if (status === 'divergent') return 'Com divergências'
  return 'Conferido'
}

function findingSeverity(severity: string): string {
  if (severity === 'blocking_error') return 'Erro bloqueante'
  if (severity === 'divergence') return 'Divergência'
  return 'Aviso'
}

function erpLabel(state: ERPSubmission['state']): string {
  if (state === 'queued') return 'Na fila'
  if (state === 'processing') return 'Processando'
  if (state === 'succeeded') return 'Cadastro simulado concluído'
  return 'Falhou'
}
