CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permission_packages (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  dce_group_id TEXT NOT NULL UNIQUE,
  dce_group_name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  target_summary TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  last_validated_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  dce_user_id TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'dce',
  is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  permission_package_id TEXT REFERENCES permission_packages(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provisioning_requests (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('CREATING','CREATED','UNKNOWN','FAILED')),
  dce_user_id TEXT,
  created_by TEXT NOT NULL REFERENCES users(id),
  error_detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('SINGLE','MULTIPLE','TRUE_FALSE')),
  stem TEXT NOT NULL,
  options TEXT NOT NULL,
  correct_options TEXT NOT NULL,
  explanation TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL,
  critical INTEGER NOT NULL DEFAULT 0 CHECK (critical IN (0, 1)),
  source_url TEXT NOT NULL DEFAULT '',
  source_section TEXT NOT NULL DEFAULT '',
  product_version TEXT NOT NULL DEFAULT '',
  reviewer TEXT NOT NULL DEFAULT '',
  reviewed_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exam_templates (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  pass_score INTEGER NOT NULL CHECK (pass_score BETWEEN 1 AND 100),
  critical_must_all_correct INTEGER NOT NULL DEFAULT 1 CHECK (critical_must_all_correct IN (0, 1)),
  duration_minutes INTEGER NOT NULL DEFAULT 30 CHECK (duration_minutes > 0),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  question_count INTEGER NOT NULL CHECK (question_count >= 0),
  status TEXT NOT NULL CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
  created_at TEXT NOT NULL,
  UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS exam_template_questions (
  exam_template_id TEXT NOT NULL REFERENCES exam_templates(id),
  question_id TEXT NOT NULL REFERENCES questions(id),
  weight INTEGER NOT NULL DEFAULT 1 CHECK (weight > 0),
  position INTEGER NOT NULL CHECK (position > 0),
  PRIMARY KEY (exam_template_id, question_id),
  UNIQUE (exam_template_id, position)
);

CREATE TABLE IF NOT EXISTS assignments (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  exam_template_id TEXT REFERENCES exam_templates(id),
  permission_package_id TEXT REFERENCES permission_packages(id),
  purpose TEXT NOT NULL CHECK (purpose IN ('ONBOARDING_GATE','TRAINING_ONLY','DIRECT_ACTIVATION')),
  status TEXT NOT NULL CHECK (status IN ('PENDING','PASSED_PENDING_GRANT','ACTIVE','COMPLETED','EXPIRED','LOCKED','CANCELLED')),
  pass_score INTEGER NOT NULL CHECK (pass_score BETWEEN 1 AND 100),
  max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
  duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
  deadline TEXT,
  created_by TEXT NOT NULL REFERENCES users(id),
  passed_at TEXT,
  activated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assignments_user_status ON assignments(user_id, status);

CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  assignment_id TEXT NOT NULL REFERENCES assignments(id),
  attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
  status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS','SUBMITTED','ABANDONED')),
  started_at TEXT NOT NULL,
  submitted_at TEXT,
  score INTEGER CHECK (score BETWEEN 0 AND 100),
  passed INTEGER CHECK (passed IN (0, 1)),
  critical_failed INTEGER CHECK (critical_failed IN (0, 1)),
  UNIQUE (assignment_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS attempt_questions (
  attempt_id TEXT NOT NULL REFERENCES attempts(id),
  question_id TEXT NOT NULL REFERENCES questions(id),
  position INTEGER NOT NULL CHECK (position > 0),
  option_order TEXT NOT NULL,
  PRIMARY KEY (attempt_id, question_id),
  UNIQUE (attempt_id, position)
);

CREATE TABLE IF NOT EXISTS attempt_answers (
  attempt_id TEXT NOT NULL REFERENCES attempts(id),
  question_id TEXT NOT NULL REFERENCES questions(id),
  selected_options TEXT NOT NULL,
  correct INTEGER NOT NULL CHECK (correct IN (0, 1)),
  PRIMARY KEY (attempt_id, question_id)
);

CREATE TABLE IF NOT EXISTS grant_jobs (
  id TEXT PRIMARY KEY,
  assignment_id TEXT NOT NULL UNIQUE REFERENCES assignments(id),
  state TEXT NOT NULL CHECK (state IN ('QUEUED','RUNNING','SUCCEEDED','RETRYABLE_FAILED','PERMANENT_FAILED')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_retry_at TEXT,
  last_error_detail TEXT NOT NULL DEFAULT '',
  locked_at TEXT,
  locked_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grant_jobs_state ON grant_jobs(state, next_retry_at);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  actor_user_id TEXT REFERENCES users(id),
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  result TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at DESC);

CREATE TABLE IF NOT EXISTS oidc_logout_hints (
  id TEXT PRIMARY KEY,
  id_token TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logout_hints_created_at ON oidc_logout_hints(created_at);

