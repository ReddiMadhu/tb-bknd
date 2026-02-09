"""SQLite database initialization and management"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from api.config import config
from loguru import logger


DATABASE_SCHEMA = """
-- Jobs table
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT CHECK(status IN ('pending', 'running', 'completed', 'failed', 'cancelled')) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    progress_percent INTEGER DEFAULT 0 CHECK(progress_percent >= 0 AND progress_percent <= 100),
    current_stage TEXT,
    error_message TEXT,
    file_count INTEGER NOT NULL,
    relationship_count INTEGER,
    result_file_path TEXT
);

-- Uploaded files table
CREATE TABLE IF NOT EXISTS uploaded_files (
    file_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Job progress logs table
CREATE TABLE IF NOT EXISTS job_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    message TEXT,
    percent INTEGER CHECK(percent >= 0 AND percent <= 100),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Preview sessions table
CREATE TABLE IF NOT EXISTS preview_sessions (
    preview_id TEXT PRIMARY KEY,
    status TEXT CHECK(status IN ('preview_ready', 'confirmed', 'cancelled')) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_count INTEGER NOT NULL,
    total_duplicates_detected INTEGER DEFAULT 0
);

-- Preview files table
CREATE TABLE IF NOT EXISTS preview_files (
    file_id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL REFERENCES preview_sessions(preview_id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    dataframe_pickle_path TEXT,
    row_count INTEGER,
    column_count INTEGER,
    metadata_json TEXT
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_uploaded_files_job_id ON uploaded_files(job_id);
CREATE INDEX IF NOT EXISTS idx_job_progress_job_id ON job_progress(job_id);
CREATE INDEX IF NOT EXISTS idx_job_progress_timestamp ON job_progress(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_preview_files_preview_id ON preview_files(preview_id);
CREATE INDEX IF NOT EXISTS idx_preview_sessions_created_at ON preview_sessions(created_at DESC);

-- ============================================================
-- TABLEAU MIGRATION TABLES
-- ============================================================

-- Migration jobs tracking
CREATE TABLE IF NOT EXISTS migration_jobs (
    migration_id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
    status TEXT CHECK(status IN ('pending', 'parsing', 'discovering', 'converting', 'validating', 'completed', 'failed')) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    progress_percent INTEGER DEFAULT 0 CHECK(progress_percent >= 0 AND progress_percent <= 100),
    current_stage TEXT,
    error_message TEXT,
    workbook_count INTEGER DEFAULT 0,
    calculation_count INTEGER DEFAULT 0,
    relationship_count INTEGER DEFAULT 0
);

-- Tableau workbooks metadata
CREATE TABLE IF NOT EXISTS tableau_workbooks (
    workbook_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL REFERENCES migration_jobs(migration_id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    file_path TEXT,
    worksheet_count INTEGER DEFAULT 0,
    dashboard_count INTEGER DEFAULT 0,
    data_source_count INTEGER DEFAULT 0,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tableau calculations extraction
CREATE TABLE IF NOT EXISTS tableau_calculations (
    calc_id TEXT PRIMARY KEY,
    workbook_id TEXT NOT NULL REFERENCES tableau_workbooks(workbook_id) ON DELETE CASCADE,
    calc_name TEXT NOT NULL,
    calc_formula TEXT NOT NULL,
    calc_type TEXT CHECK(calc_type IN ('CALCULATED_FIELD', 'MEASURE', 'PARAMETER', 'LOD', 'TABLE_CALC')) NOT NULL,
    visual_context TEXT,  -- JSON string
    dependency_level INTEGER DEFAULT 0,
    used_in_worksheets TEXT,  -- Comma-separated list
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- DAX conversion results
CREATE TABLE IF NOT EXISTS dax_conversions (
    conversion_id TEXT PRIMARY KEY,
    calc_id TEXT NOT NULL REFERENCES tableau_calculations(calc_id) ON DELETE CASCADE,
    migration_id TEXT NOT NULL REFERENCES migration_jobs(migration_id) ON DELETE CASCADE,
    dax_formula TEXT NOT NULL,
    conversion_method TEXT CHECK(conversion_method IN ('LLM_PATTERN', 'LLM_GENERATED', 'RULE_BASED', 'MANUAL_OVERRIDE')) DEFAULT 'LLM_PATTERN',
    confidence_score REAL CHECK(confidence_score >= 0 AND confidence_score <= 1),
    reasoning TEXT,  -- LLM reasoning
    warnings TEXT,  -- JSON array of warning messages
    status TEXT CHECK(status IN ('pending', 'validated', 'failed', 'manual_review')) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Validation results tracking
CREATE TABLE IF NOT EXISTS validation_results (
    validation_id TEXT PRIMARY KEY,
    conversion_id TEXT NOT NULL REFERENCES dax_conversions(conversion_id) ON DELETE CASCADE,
    test_slice TEXT,  -- JSON string with dimension values
    tableau_value REAL,
    dax_value REAL,
    delta REAL,
    relative_error REAL,
    passed BOOLEAN NOT NULL,
    error_category TEXT CHECK(error_category IN ('PERFECT_MATCH', 'ROUNDING_ERROR', 'NULL_HANDLING', 'CONTEXT_SHIFT', 'SCALE_ERROR', 'AGGREGATION_MISMATCH', 'MISSING_VALUE')),
    correction_attempts INTEGER DEFAULT 0,
    validated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Conversion patterns library (optional - can also use YAML)
CREATE TABLE IF NOT EXISTS conversion_patterns (
    pattern_id TEXT PRIMARY KEY,
    pattern_name TEXT NOT NULL,
    tableau_formula TEXT NOT NULL,
    dax_formula TEXT NOT NULL,
    context TEXT,  -- JSON string
    confidence REAL DEFAULT 1.0,
    tags TEXT,  -- Comma-separated tags
    notes TEXT,
    usage_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP
);

-- Migration progress tracking (detailed)
CREATE TABLE IF NOT EXISTS migration_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_id TEXT NOT NULL REFERENCES migration_jobs(migration_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    message TEXT,
    percent INTEGER CHECK(percent >= 0 AND percent <= 100),
    details TEXT,  -- JSON string with additional context
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Migration indexes
CREATE INDEX IF NOT EXISTS idx_migration_jobs_status ON migration_jobs(status);
CREATE INDEX IF NOT EXISTS idx_migration_jobs_created_at ON migration_jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tableau_workbooks_migration_id ON tableau_workbooks(migration_id);
CREATE INDEX IF NOT EXISTS idx_tableau_calculations_workbook_id ON tableau_calculations(workbook_id);
CREATE INDEX IF NOT EXISTS idx_dax_conversions_migration_id ON dax_conversions(migration_id);
CREATE INDEX IF NOT EXISTS idx_dax_conversions_status ON dax_conversions(status);
CREATE INDEX IF NOT EXISTS idx_validation_results_conversion_id ON validation_results(conversion_id);
CREATE INDEX IF NOT EXISTS idx_validation_results_passed ON validation_results(passed);
CREATE INDEX IF NOT EXISTS idx_migration_progress_migration_id ON migration_progress(migration_id);
CREATE INDEX IF NOT EXISTS idx_migration_progress_timestamp ON migration_progress(timestamp DESC);

-- ============================================================
-- 100% FIDELITY VALIDATION TABLES
-- ============================================================

-- High-fidelity validation results
CREATE TABLE IF NOT EXISTS fidelity_validations (
    validation_id TEXT PRIMARY KEY,
    migration_id TEXT REFERENCES migration_jobs(migration_id) ON DELETE CASCADE,
    conversion_id TEXT,
    overall_passed BOOLEAN NOT NULL,
    pass_rate REAL NOT NULL CHECK(pass_rate >= 0 AND pass_rate <= 1),
    correction_attempts INTEGER DEFAULT 0,
    final_dax TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Test slices for numerical comparison
CREATE TABLE IF NOT EXISTS validation_test_slices (
    slice_id TEXT PRIMARY KEY,
    validation_id TEXT NOT NULL REFERENCES fidelity_validations(validation_id) ON DELETE CASCADE,
    dimensions TEXT NOT NULL,  -- JSON: {"Region": "East", "Year": 2024}
    tableau_value REAL,
    dax_value REAL,
    delta REAL,
    relative_error REAL,
    passed BOOLEAN NOT NULL,
    error_category TEXT CHECK(error_category IN (
        'PERFECT_MATCH', 'ROUNDING_ERROR', 'SCALE_ERROR',
        'NULL_HANDLING', 'CONTEXT_SHIFT', 'GRAIN_MISMATCH',
        'AGGREGATION_MISMATCH', 'MISSING_VALUE'
    ))
);

-- Self-healing agent correction attempts
CREATE TABLE IF NOT EXISTS correction_attempts (
    attempt_id TEXT PRIMARY KEY,
    validation_id TEXT NOT NULL REFERENCES fidelity_validations(validation_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    original_dax TEXT NOT NULL,
    corrected_dax TEXT NOT NULL,
    root_cause TEXT,
    explanation TEXT,
    changes_made TEXT,  -- JSON array: ["Change 1", "Change 2"]
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Fidelity validation indexes
CREATE INDEX IF NOT EXISTS idx_fidelity_validations_migration_id ON fidelity_validations(migration_id);
CREATE INDEX IF NOT EXISTS idx_fidelity_validations_conversion_id ON fidelity_validations(conversion_id);
CREATE INDEX IF NOT EXISTS idx_validation_test_slices_validation_id ON validation_test_slices(validation_id);
CREATE INDEX IF NOT EXISTS idx_validation_test_slices_passed ON validation_test_slices(passed);
CREATE INDEX IF NOT EXISTS idx_correction_attempts_validation_id ON correction_attempts(validation_id);
"""


def init_database():
    """Initialize the SQLite database with schema"""
    try:
        # Ensure directory exists
        db_path = Path(config.DATABASE_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Connect and create schema
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")  # Enable foreign key constraints
        conn.executescript(DATABASE_SCHEMA)
        conn.commit()
        conn.close()

        logger.info(f"Database initialized at {db_path}")

    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise


@contextmanager
def get_db_connection():
    """
    Context manager for database connections

    Usage:
        with get_db_connection() as conn:
            cursor = conn.execute("SELECT ...")
            rows = cursor.fetchall()
    """
    conn = None
    try:
        conn = sqlite3.connect(
            config.DATABASE_PATH,
            check_same_thread=False,  # Allow usage from different threads
            timeout=30.0  # 30 second timeout
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row  # Enable column access by name
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        if conn:
            conn.close()


def execute_query(query: str, params: tuple = (), fetch_one: bool = False):
    """
    Execute a query and return results

    Args:
        query: SQL query string
        params: Query parameters
        fetch_one: If True, return single row; otherwise return all rows

    Returns:
        Single row (if fetch_one=True) or list of rows
    """
    with get_db_connection() as conn:
        cursor = conn.execute(query, params)
        if fetch_one:
            return cursor.fetchone()
        return cursor.fetchall()


def execute_update(query: str, params: tuple = ()):
    """
    Execute an INSERT/UPDATE/DELETE query

    Args:
        query: SQL query string
        params: Query parameters

    Returns:
        Number of affected rows
    """
    with get_db_connection() as conn:
        cursor = conn.execute(query, params)
        return cursor.rowcount


def cleanup_old_jobs(days: int = 7):
    """
    Delete jobs older than specified days

    Args:
        days: Number of days to keep jobs
    """
    query = """
        DELETE FROM jobs
        WHERE created_at < datetime('now', '-' || ? || ' days')
        AND status IN ('completed', 'failed', 'cancelled')
    """
    deleted = execute_update(query, (days,))
    logger.info(f"Cleaned up {deleted} old jobs")
    return deleted


def cleanup_old_previews(hours: int = 1):
    """
    Delete preview sessions older than specified hours

    Args:
        hours: Number of hours to keep previews (default 1 hour)
    """
    query = """
        DELETE FROM preview_sessions
        WHERE created_at < datetime('now', '-' || ? || ' hours')
    """
    deleted = execute_update(query, (hours,))
    logger.info(f"Cleaned up {deleted} old preview sessions")
    return deleted


def cleanup_old_migrations(days: int = 30):
    """
    Delete migration jobs older than specified days

    Args:
        days: Number of days to keep migrations (default 30 days)
    """
    query = """
        DELETE FROM migration_jobs
        WHERE created_at < datetime('now', '-' || ? || ' days')
        AND status IN ('completed', 'failed')
    """
    deleted = execute_update(query, (days,))
    logger.info(f"Cleaned up {deleted} old migration jobs")
    return deleted
