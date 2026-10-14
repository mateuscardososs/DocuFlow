export type Role = 'operator' | 'approver'

export interface WorkspaceAccess {
  id: number
  slug: string
  name: string
  roles: Role[]
}

export interface UserProfile {
  id: number
  email: string
  workspaces: WorkspaceAccess[]
}

export interface ExtractedField {
  raw_value: string | null
  value: string | null
  page: number | null
  evidence: string | null
}

export interface DocumentItem {
  code: ExtractedField
  description: ExtractedField
  quantity: ExtractedField
  unit_price: ExtractedField
  line_total: ExtractedField
}

export interface ReferenceDocument {
  supplier_name: ExtractedField
  supplier_identifier: ExtractedField
  document_number: ExtractedField
  issue_date: ExtractedField
  purchase_order_reference: ExtractedField
  currency: ExtractedField
  items: DocumentItem[]
  document_total: ExtractedField
  unsupported_elements: string[]
}

export type ProcessingStatus = 'queued' | 'processing' | 'ready_for_review' | 'failed'
export type ReviewStatus = 'pending' | 'approved' | 'rejected'
export type DocumentStatus = 'blocked' | 'divergent' | 'ready_for_review' | null

export interface Finding {
  severity: 'blocking_error' | 'divergence' | 'warning'
  code: string
  message: string
}

export interface ValidationReport {
  status: Exclude<DocumentStatus, null>
  findings: Finding[]
  commercial_policy: {
    version: number
    default_total_tolerance: string
    currency_tolerances: Record<string, string>
  }
}

export interface CommercialPolicy {
  version: number
  default_total_tolerance: string
  currency_tolerances: Record<string, string>
  created_by_user_id: number | null
  created_by_email: string | null
  created_at: string | null
}

export interface DocumentSummary {
  id: number
  workspace_id: number
  purchase_order_id: number
  assignee_user_id: number | null
  assignee_email: string | null
  assigned_by_user_id: number | null
  assigned_by_email: string | null
  assigned_at: string | null
  assignment_status: 'assigned' | 'unassigned'
  purchase_order_reference?: string | null
  supplier_name?: string | null
  original_filename: string
  size_bytes: number
  content_type: string
  sha256: string
  uploaded_at: string
  processing_status: ProcessingStatus
  review_status: ReviewStatus
  review_version: number
  processing_error: string | null
  final_status: DocumentStatus
}

export interface DocumentDetail extends DocumentSummary {
  page_texts: Array<{ number: number; text: string; source: 'native' | 'ocr' }> | null
  extracted_document: ReferenceDocument | null
  reviewed_document: ReferenceDocument | null
  pdf_warnings: string[] | null
  validation_result: ValidationReport | null
  approval: {
    approved_version: number
    approver_user_id: number
    approved_at: string
    approved_document: ReferenceDocument
    validation_result: ValidationReport
  } | null
}

export interface DocumentAnalysisResponse {
  document_id: number
  duplicate: boolean
  processing_status: ProcessingStatus
  state: ProcessingStatus
  review_status: ReviewStatus
  review_version: number
  processing_error: string | null
  document: ReferenceDocument | null
  pdf_warnings: string[] | null
  validation: ValidationReport | null
  status: DocumentStatus
}

export interface DocumentReviewResponse {
  document_id: number
  review_status: ReviewStatus
  review_version: number
  reviewed_document: ReferenceDocument
  validation: ValidationReport
  approval: DocumentDetail['approval']
}

export interface AuditEvent {
  id: number
  event_type: 'review' | 'correction' | 'approval' | 'rejection' | 'erp_submission' | 'erp_attempt' | 'erp_success' | 'erp_failure' | 'assignment'
  version: number
  actor_user_id: number
  justification: string | null
  details: Record<string, unknown> | null
  created_at: string
}

export interface WorkspaceMember {
  id: number
  email: string
  roles: Role[]
}

export interface DocumentAssignment {
  document_id: number
  assignee_user_id: number | null
  assignee_email: string | null
  assigned_by_user_id: number | null
  assigned_by_email: string | null
  assigned_at: string | null
  assignment_status: 'assigned' | 'unassigned'
}

export interface ERPSubmission {
  id: number
  document_id: number
  approved_version: number
  state: 'queued' | 'processing' | 'succeeded' | 'failed'
  attempts: number
  erp_identifier: string | null
  failure_message: string | null
  requested_at: string
  updated_at: string
}

export interface WorkspaceDashboard {
  start_date: string
  end_date: string
  documents: {
    queued: number
    processing: number
    awaiting_review: number
    awaiting_approval: number
    processing_failed: number
  }
  erp_submissions: { pending: number; succeeded: number; failed: number }
  volume_total: number
  document_volume: Array<{ date: string; count: number }>
  common_divergences: Array<{ code: string; count: number }>
}

export interface PurchaseOrderImportLineError {
  line_number: number
  reason: string
}

export interface PurchaseOrderImportPreviewOrder {
  reference: string
  supplier_identifier: string
  supplier_name: string
  currency: string
  total: string
  item_count: number
  line_numbers: number[]
  outcome: 'new' | 'duplicate' | 'conflict'
}

export interface PurchaseOrderImportPreview {
  total_rows: number
  valid_rows: number
  orders: PurchaseOrderImportPreviewOrder[]
  errors: PurchaseOrderImportLineError[]
}

export interface PurchaseOrderImportConfirmation {
  total_rows: number
  valid_rows: number
  imported: string[]
  ignored_duplicates: string[]
  errors: PurchaseOrderImportLineError[]
}
