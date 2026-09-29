/** Shapes returned by /api/v1, transcribed from live responses (§13.1). */

export type Whoami = {
  user_id: string;
  tenant_id: string;
  display_name: string;
  issuer: string;
  roles: Record<string, string>;
  grants_by_project: Record<string, unknown>;
  permissions_by_project: Record<string, string[]>;
  is_admin: boolean;
  can_manage_users: boolean;
  request_id: string;
};

export type Capabilities = {
  api_version: string;
  ir_version: string;
  compiler_version: string;
  actions: string[];
  browsers: string[];
  conditions: string[];
  features: Record<string, string>;
  limits: Record<string, number>;
  auth_mode: string;
};

export type Project = {
  id: string;
  name: string;
  display_name: string;
  description: string | null;
  quota: Record<string, number>;
  settings: Record<string, unknown>;
  archived: boolean;
  row_version: number;
  role: string;
  created_at: string;
  updated_at: string;
};

export type CaseRow = {
  case_id: string;
  project_id: string;
  name: string;
  title: string | null;
  tags: string[];
  current_revision_id: string | null;
  revision_no: number;
  source_digest: string;
  compile_status: string;
  archived: boolean;
  row_version: number;
  created_at: string;
  updated_at: string;
  etag: string;
};

export type Revision = {
  revision_id: string;
  case_id: string;
  version: number;
  title: string | null;
  dsl_version: string;
  source_digest: string;
  bytes: number;
  created_by: string;
  created_at: string;
};

export type Diagnostic = {
  code: string;
  severity: string;
  message: string;
  source_range?: { start_line: number; end_line: number } | null;
  hint?: string | null;
};

export type CompileArtifact = {
  compile_artifact_id: string;
  revision_id: string;
  project_id: string;
  status: string;
  compiler_mode: string;
  compiler_version: string;
  model: string | null;
  prompt_version: string | null;
  source_digest: string;
  ir_digest: string | null;
  error_code: string | null;
  diagnostics: Diagnostic[];
  review_items: { code: string; message: string; step_id?: string | null }[];
  usage: { calls: number; prompt_tokens: number; completion_tokens: number; latency_ms: number; model: string | null };
  confirmed_by: string | null;
  confirmed_at: string | null;
  created_at: string;
  executable: boolean;
  ir: Record<string, any> | null;
};

export type CaseDetail = CaseRow & {
  description: string | null;
  markdown: string;
  current_revision: Revision | null;
  compile: {
    artifact_id: string;
    status: string;
    compiler_mode: string;
    model: string | null;
    diagnostics: Diagnostic[];
    review_items: CompileArtifact["review_items"];
    ir: Record<string, any> | null;
    ir_digest: string | null;
    confirmed_at: string | null;
  } | null;
};

export type EnvironmentRevision = {
  environment_revision_id: string;
  environment_id: string;
  version: number;
  digest: string;
  config: {
    base_url: string;
    allowed_domains: string[];
    allowed_protocols?: string[];
    browsers: string[];
    viewport: { width: number; height: number };
    evidence: { mode: string; trace: string; video: string };
    variables: Record<string, unknown>;
  };
  secret_bindings: Record<string, unknown>;
  current: boolean;
  created_by: string;
  created_at: string;
};

export type Environment = {
  environment_id: string;
  project_id: string;
  name: string;
  row_version: number;
  etag: string;
  archived_at: string | null;
  current_revision: EnvironmentRevision | null;
};

export type ExecutionSummary = {
  id: string;
  project_id: string;
  case_id: string;
  case_name: string;
  revision_no: number;
  status: string;
  outcome: string | null;
  error_code: string | null;
  trigger: string;
  browser: string;
  browser_version: string | null;
  evidence_mode: string;
  artifact_status: string | null;
  analysis_status: string | null;
  queued_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  active_ms: number | null;
  human_ms: number | null;
  human_tasks_used: number;
  cancel_requested: boolean;
  retry_of: string | null;
};

export type ExecutionStep = {
  step_id: string;
  step_no: number;
  action: string;
  description: string | null;
  status: string;
  duration_ms: number | null;
  error_code: string | null;
};

export type StepDetail = ExecutionStep & {
  dispatch_state?: string;
  locator_strategy: string | null;
  locator_attempts: {
    strategy: string;
    source: string;
    matched: number;
    outcome: string;
    elapsed_ms: number;
    selector?: string | null;
  }[];
  error?: Record<string, any> | null;
  resume_phase: string | null;
  passed_by_human: boolean;
  artifacts: EvidenceItem[];
  started_at: string | null;
  ended_at: string | null;
};

export type EvidenceItem = {
  ref: string;
  kind: string;
  name: string;
  step_id: string | null;
  media_type: string;
  size: number;
  sha256: string;
  sensitivity: string;
  upload_status: string;
  available: boolean;
  requires_authorization: boolean;
  expires_at: string | null;
  metadata: Record<string, unknown>;
};

export type ExecutionDetail = ExecutionSummary & {
  state_version: number;
  last_event_seq: number;
  environment_id: string | null;
  environment_revision_id: string | null;
  compile_artifact_id: string | null;
  revision_id: string | null;
  requested_by: string;
  error: Record<string, any> | null;
  step_summary: { total: number; by_status: Record<string, number>; passed: number; failed: number };
  steps: ExecutionStep[];
  human_task: HumanTask | null;
  report_url: string;
  events_url: string;
};

export type Analysis = {
  status: string;
  revision: number;
  failure_type: string | null;
  reason: string | null;
  suggestion: string | null;
  confidence: number | null;
  evidence_refs: string[];
  is_hypothesis: boolean;
  source: string;
  model: string | null;
  prompt_version: string | null;
  error_code: string | null;
  history: { revision: number; source: string; status: string; failure_type: string; confidence: number; is_hypothesis: boolean }[];
} | null;

export type Report = {
  execution: {
    id: string;
    status: string;
    outcome: string | null;
    error_code: string | null;
    error_detail: Record<string, any>;
    trigger: string;
    requested_by: string;
    retry_of: string | null;
    browser: string;
    browser_version: string | null;
    evidence_mode: string;
    artifact_status: string | null;
    analysis_status: string | null;
    cleanup_status: string;
    cancelled: boolean;
    queued_at: string | null;
    started_at: string | null;
    ended_at: string | null;
    durations: { queued_ms: number; active_ms: number; human_ms: number; total_ms: number };
    human_tasks_used: number;
  };
  case: {
    case_id: string;
    case_name: string;
    revision_id: string;
    revision_no: number;
    source_digest: string;
    ir_digest: string | null;
    ir_version: string;
    step_count: number;
    compile: Record<string, any>;
  };
  environment: {
    environment_id: string;
    environment_revision_id: string;
    base_url: string;
    allowed_domains: string[];
    browsers: string[];
    viewport: { width: number; height: number };
    evidence: Record<string, string>;
    variables: unknown[];
    secret_bindings: unknown[];
    run_variables: unknown[];
    evidence_mode: string;
  };
  steps: StepDetail[];
  evidence: {
    status: string;
    total: number;
    by_kind: Record<string, number>;
    bytes: number;
    items: EvidenceItem[];
    missing: string[];
    privacy_excluded_kinds: string[];
    expired: string[];
    access: string;
    budget_bytes: number;
  };
  human: { human_task_id: string; step_id: string; reason: string; status: string; started_at: string; ended_at: string | null; note: string | null }[];
  analysis: Analysis | null;
  analysis_ready: boolean;
  report_phase: string;
  generated_at: string;
  warnings: string[];
};

export type HumanTask = {
  human_task_id: string;
  execution_id: string;
  project_id: string;
  step_id: string;
  reason: string;
  mode: Record<string, any> | string;
  status: string;
  controller_id: string | null;
  control_lease_until: string | null;
  holds_control: boolean;
  deadline: string;
  remaining_seconds: number;
  session_epoch: number;
  resume_phase: string | null;
  resume_condition: string | null;
  resume_requested_at: string | null;
  outcome_note: string | null;
  created_at: string | null;
  control_channel: string;
};

export type Metrics = {
  window: { since: string | null; until: string; generated_at: string };
  scope: { project_id: string; environment_id: string | null; case_id: string | null };
  total: number;
  by_outcome: Record<string, number>;
  passed: number;
  failed: number;
  pass_rate: number;
  pass_rate_denominator: number;
  infrastructure: { ERROR: number; TIMED_OUT: number; CANCELLED: number };
  avg_active_ms: number;
  p95_active_ms: number;
  human_interventions: number;
  human_intervention_rate: number;
  human_ms_total: number;
  locator: { located_steps: number; degraded_steps: number; degradation_rate: number };
  failure_types: Record<string, number>;
};

export type Member = { user_id: string; role: string; display_name: string; subject: string; status: string; joined_at: string };
export type Grant = {
  user_id: string;
  project_id: string;
  permission: string;
  granted_by: string | null;
  reason: string | null;
  expires_at: string | null;
  created_at: string;
};
export type AuditRow = {
  audit_id: string;
  actor_id: string;
  actor_name: string | null;
  project_id: string;
  operation: string;
  resource_type: string;
  resource_id: string;
  request_id: string | null;
  detail: Record<string, unknown>;
  created_at: string;
};
export type UserRow = {
  id: string;
  issuer: string;
  subject: string;
  display_name: string;
  email: string | null;
  status: string;
  tenants: string[];
  tenant_role: string;
  created_at: string;
};
export type SecretRow = {
  logical_name: string;
  version: number;
  created_at: string;
  [key: string]: unknown;
};
