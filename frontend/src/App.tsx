import { useCallback, useEffect, useMemo, useState, useId, useRef, type FormEvent } from 'react'
import { ApiError, DocuFlowApi } from './api'
import type {
  AuditEvent,
  DocumentDetail,
  DocumentSummary,
  ExtractedField,
  ERPSubmission,
  ReferenceDocument,
  UserProfile,
  WorkspaceAccess,
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
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [notice, setNotice] = useState('')
  const [uploadBusy, setUploadBusy] = useState(false)
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
      const result = await api.listDocuments(requestedWorkspaceId)
      if (activeWorkspaceId.current === requestedWorkspaceId) setDocuments(result)
    } catch (error) {
      if (activeWorkspaceId.current === requestedWorkspaceId) setListError(messageFrom(error))
    } finally {
      if (activeWorkspaceId.current === requestedWorkspaceId) setListBusy(false)
    }
  }, [api, workspace])

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
          <span className="nav-item active"><span aria-hidden="true">▤</span> Documentos</span>
          <span className="nav-item muted-nav"><span aria-hidden="true">◷</span> Histórico de revisão</span>
        </div>
        <div className="sidebar-bottom">
          <div className="user-chip"><span className="avatar">{session.profile.email.slice(0, 1).toUpperCase()}</span><span><strong>{session.profile.email}</strong><small>{roles.map(roleLabel).join(' · ')}</small></span></div>
          <button className="button ghost signout" type="button" onClick={onLogout}>Sair da conta</button>
        </div>
      </aside>

      <section className="main-area">
        <header className="topbar">
          <div><span className="breadcrumb">Workspace / {workspace?.name}</span><h1>Documentos</h1></div>
          <div className="topbar-meta"><span className="secure-label"><span aria-hidden="true">●</span> Ambiente seguro</span><span className="avatar top-avatar">{session.profile.email.slice(0, 1).toUpperCase()}</span></div>
        </header>

        <div className="content-wrap">
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
              <label className="date-filter"><span className="sr-only">Data inicial</span><input aria-label="Data inicial" type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} /></label>
              <label className="date-filter"><span className="sr-only">Data final</span><input aria-label="Data final" type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} /></label>
            </div>

            {listError && <div className="alert error" role="alert">{listError}</div>}
            {listBusy ? <div className="loading-state" role="status">Carregando documentos…</div> : visibleDocuments.length ? (
              <div className="document-table-wrap"><table className="document-table">
                <thead><tr><th>Documento</th><th>Fornecedor / pedido</th><th>Enviado em</th><th>Processamento</th><th>Revisão</th><th><span className="sr-only">Abrir</span></th></tr></thead>
                <tbody>{visibleDocuments.map((document) => <tr key={document.id}>
                  <td><button className="document-link" type="button" onClick={() => setSelectedId(document.id)}><span className="file-icon" aria-hidden="true">▧</span><span><strong>{document.original_filename}</strong><small>#{document.id} · {fileSize(document.size_bytes)}</small></span></button></td>
                  <td><span className="table-primary">{document.supplier_name || 'Fornecedor pendente'}</span><small className="table-secondary">{document.purchase_order_reference || `Pedido #${document.purchase_order_id}`}</small></td>
                  <td className="date-cell">{formatDate(document.uploaded_at)}</td>
                  <td><StatusPill kind={document.processing_status === 'failed' ? 'danger' : isPending(document.processing_status) ? 'progress' : 'neutral'}>{processingLabels[document.processing_status]}</StatusPill>{document.processing_error && <small className="failure-hint">{document.processing_error}</small>}</td>
                  <td><StatusPill kind={document.review_status === 'approved' ? 'success' : document.review_status === 'rejected' || document.processing_status === 'failed' ? 'danger' : document.processing_status === 'ready_for_review' ? 'review' : 'neutral'}>{reviewLabelFor(document)}</StatusPill></td>
                  <td><button className="icon-button" type="button" aria-label={`Abrir ${document.original_filename}`} onClick={() => setSelectedId(document.id)}>→</button></td>
                </tr>)}</tbody>
              </table></div>
            ) : <div className="empty-state"><div className="empty-icon" aria-hidden="true">▤</div><h3>Nenhum documento encontrado</h3><p>Altere os filtros ou envie um documento para começar.</p></div>}
          </section>
          <footer className="footer-note">A extração auxilia a conferência. A decisão final sempre depende de revisão humana.</footer>
        </div>
      </section>
    </main>
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
          </div>

          {document.processing_status === 'failed' && <div className="alert error"><strong>Falha no processamento.</strong> {document.processing_error || 'Tente processar o documento novamente mais tarde.'}</div>}
          {isPending(document.processing_status) && <div className="processing-banner"><span className="spinner" aria-hidden="true" /><span>{document.processing_status === 'queued' ? 'Documento na fila. Atualizando o andamento automaticamente…' : 'Extraindo e validando o documento…'}</span></div>}

          <div className="detail-grid">
            <div className="detail-main-column">
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

              <section className="panel" aria-labelledby="pages-heading">
                <div className="panel-heading"><div><p className="eyebrow">RASTREABILIDADE</p><h2 id="pages-heading">Texto por página</h2></div></div>
                {document.page_texts?.length ? <div className="page-list">{document.page_texts.map((page) => <article className="page-card" key={page.number}><header><strong>Página {page.number}</strong><StatusPill kind={page.source === 'ocr' ? 'warning' : 'neutral'}>{page.source === 'ocr' ? 'OCR' : 'Texto nativo'}</StatusPill></header><p>{page.text || 'Nenhum texto utilizável nesta página.'}</p></article>)}</div> : <div className="empty-inline">O texto por página ainda não está disponível.</div>}
              </section>
            </div>

            <aside className="detail-side-column">
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

function ExtractedFieldView({ label, field, editable, compact = false, onChange }: {
  label: string
  field: ExtractedField
  editable: boolean
  compact?: boolean
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
        {field.evidence && <span className="evidence-quote">“{field.evidence}”</span>}
      </>}
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
