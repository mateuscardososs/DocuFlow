import type {
  AuditEvent,
  DocumentAnalysisResponse,
  DocumentDetail,
  DocumentReviewResponse,
  DocumentSummary,
  ERPSubmission,
  ReferenceDocument,
  UserProfile,
} from './types'

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export class DocuFlowApi {
  private readonly token: string | null

  constructor(token: string | null = null) {
    this.token = token
  }

  async login(email: string, password: string): Promise<{ access_token: string; token_type: 'bearer'; expires_in: number }> {
    const form = new URLSearchParams({ username: email, password })
    return this.request('/auth/token', { method: 'POST', body: form })
  }

  me(token: string): Promise<UserProfile> {
    return new DocuFlowApi(token).request('/auth/me')
  }

  listDocuments(workspaceId: number): Promise<DocumentSummary[]> {
    return this.workspaceRequest(workspaceId, '/documents?limit=100&offset=0')
  }

  getDocument(workspaceId: number, documentId: number): Promise<DocumentDetail> {
    return this.workspaceRequest(workspaceId, `/documents/${documentId}`)
  }

  getAudit(workspaceId: number, documentId: number): Promise<AuditEvent[]> {
    return this.workspaceRequest(workspaceId, `/documents/${documentId}/audit`)
  }

  async upload(workspaceId: number, file: File, purchaseOrderReference: string): Promise<DocumentAnalysisResponse> {
    const form = new window.FormData()
    form.append('file', file)
    form.append('purchase_order_reference', purchaseOrderReference)
    return this.workspaceRequest(workspaceId, '/documents/analyze', { method: 'POST', body: form })
  }

  correct(
    workspaceId: number,
    documentId: number,
    payload: { expected_version: number; justification: string; reviewed_document: ReferenceDocument },
  ): Promise<DocumentReviewResponse> {
    return this.workspaceRequest(workspaceId, `/documents/${documentId}/review`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  }

  approve(workspaceId: number, documentId: number, expectedVersion: number): Promise<DocumentReviewResponse> {
    return this.workspaceRequest(workspaceId, `/documents/${documentId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ expected_version: expectedVersion }),
    })
  }

  reject(workspaceId: number, documentId: number, expectedVersion: number, justification: string): Promise<DocumentReviewResponse> {
    return this.workspaceRequest(workspaceId, `/documents/${documentId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ expected_version: expectedVersion, justification }),
    })
  }

  requestERP(workspaceId: number, documentId: number, expectedVersion: number): Promise<ERPSubmission> {
    return this.workspaceRequest(workspaceId, `/documents/${documentId}/erp-submission`, {
      method: 'POST',
      body: JSON.stringify({ expected_version: expectedVersion }),
    })
  }

  getERPSubmission(workspaceId: number, submissionId: number): Promise<ERPSubmission> {
    return this.workspaceRequest(workspaceId, `/erp-submissions/${submissionId}`)
  }

  download(workspaceId: number, documentId: number): Promise<Response> {
    return this.workspaceRequest(workspaceId, `/documents/${documentId}/download`, {}, false)
  }

  private workspaceRequest<T>(workspaceId: number, path: string, init: RequestInit = {}, jsonResponse = true): Promise<T> {
    return this.request(path, init, jsonResponse, workspaceId)
  }

  private async request<T>(path: string, init: RequestInit = {}, jsonResponse = true, workspaceId?: number): Promise<T> {
    const headers = new Headers(init.headers)
    if (this.token) headers.set('Authorization', `Bearer ${this.token}`)
    if (workspaceId !== undefined) headers.set('X-Workspace-ID', String(workspaceId))
    if (typeof init.body === 'string') {
      headers.set('Content-Type', 'application/json')
    }
    const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
    if (!response.ok) {
      let message = 'Não foi possível concluir a solicitação.'
      try {
        const body = await response.json() as { detail?: unknown }
        if (typeof body.detail === 'string') message = body.detail
      } catch {
        // A resposta pode não conter JSON; não mostramos dados técnicos ao usuário.
      }
      throw new ApiError(response.status, message)
    }
    if (!jsonResponse) return response as T
    return response.json() as Promise<T>
  }
}

export const publicApi = new DocuFlowApi()
