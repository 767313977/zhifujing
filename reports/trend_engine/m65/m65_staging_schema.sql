-- M6.5 多供应商 staging schema 草案（自动生成，勿手改）
-- 生成器：scripts/m65_write_schema_draft.py
-- 落库位置：backend/data/m65_staging/<batch_id>/m65_multisource_staging.db
-- 规则：只追加、按 provider 隔离、不写原始研究库

CREATE TABLE provider_manifest (
  batch_id TEXT, provider TEXT, transport TEXT, product TEXT,
  created_at TEXT, PRIMARY KEY (batch_id, provider)
);
CREATE TABLE provider_capability (
  batch_id TEXT, provider TEXT, datasets TEXT, history_start TEXT,
  pit_precision TEXT, supports_revision INTEGER, license_scope TEXT, notes TEXT,
  PRIMARY KEY (batch_id, provider)
);
CREATE TABLE raw_payload_manifest (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, endpoint TEXT, payload_hash TEXT,
  bytes INTEGER, raw_json TEXT, ingested_at TEXT,
  UNIQUE (batch_id, provider, endpoint, payload_hash)
);
CREATE TABLE corporate_action_raw (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, product TEXT, endpoint TEXT, field_code TEXT,
  code TEXT, event_type TEXT, announcement_date TEXT, announcement_precision TEXT,
  record_date TEXT, ex_date TEXT, pay_date TEXT, share_listing_date TEXT,
  cash_per_share_pretax REAL, cash_per_share_posttax REAL,
  stock_dividend_ratio REAL, transfer_ratio REAL,
  rights_ratio REAL, rights_price REAL, rights_shares REAL,
  plan_status TEXT, revision_id TEXT, available_at TEXT, ingested_at TEXT,
  payload_hash TEXT, quality_status TEXT, license_scope TEXT, source_record_id TEXT
);
CREATE TABLE adjustment_factor_raw (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, product TEXT, endpoint TEXT, field_code TEXT,
  code TEXT, trade_date TEXT, factor REAL, factor_type TEXT, formula TEXT,
  provider_version TEXT, available_at TEXT, ingested_at TEXT,
  payload_hash TEXT, quality_status TEXT, license_scope TEXT, source_record_id TEXT
);
CREATE TABLE sector_master_raw (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, provider_sector_id TEXT, provider_sector_name TEXT,
  taxonomy TEXT, level TEXT, available_at TEXT, ingested_at TEXT, payload_hash TEXT
);
CREATE TABLE sector_membership_snapshot (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, provider_sector_id TEXT, provider_sector_name TEXT,
  taxonomy TEXT, snapshot_date TEXT, constituent_code TEXT, constituent_name TEXT,
  membership_mode TEXT, provider_asof TEXT, available_at TEXT, ingested_at TEXT,
  source_version TEXT, payload_hash TEXT, quality_status TEXT
);
CREATE TABLE sector_membership_event (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT, provider TEXT, provider_sector_id TEXT, taxonomy TEXT,
  constituent_code TEXT, in_date TEXT, out_date TEXT,
  available_at TEXT, ingested_at TEXT, source_version TEXT, payload_hash TEXT
);
CREATE TABLE sector_id_crosswalk (
  batch_id TEXT, b1_sector_id TEXT, provider TEXT, provider_sector_id TEXT,
  provider_sector_name TEXT, taxonomy TEXT, mapping_method TEXT, confidence REAL,
  valid_from TEXT, valid_to TEXT, review_status TEXT,
  PRIMARY KEY (batch_id, b1_sector_id, provider)
);
CREATE TABLE ingestion_run (
  batch_id TEXT, provider TEXT, dataset TEXT, status TEXT,
  calls INTEGER, records INTEGER, started_at TEXT, finished_at TEXT, notes TEXT,
  PRIMARY KEY (batch_id, provider, dataset)
);
CREATE TABLE ingestion_error (
  batch_id TEXT, provider TEXT, dataset TEXT, error_code TEXT, detail TEXT, created_at TEXT
);
CREATE TABLE license_manifest (
  batch_id TEXT, provider TEXT, dataset TEXT, storage_license TEXT,
  website_usage_license TEXT, notes TEXT,
  PRIMARY KEY (batch_id, provider, dataset)
);
