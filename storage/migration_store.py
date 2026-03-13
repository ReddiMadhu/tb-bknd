"""Migration job persistence and state management"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from loguru import logger
import json

from storage.database import get_db_connection
from api.models.migration_models import (
    MigrationJob,
    MigrationStatus,
    TableauWorkbook,
    TableauCalculation,
    DAXConversion,
    ValidationResult,
    CalculationType,
    ConversionMethod,
    ConversionStatus,
    ErrorCategory,
)


class MigrationStore:
    """Manages migration job persistence in SQLite database"""

    # ============================================
    # Migration Job Operations
    # ============================================

    def create_migration(
        self, migration_id: str, job_id: Optional[str] = None
    ) -> MigrationJob:
        """
        Create a new migration job

        Args:
            migration_id: Unique migration identifier
            job_id: Optional associated job ID

        Returns:
            Created MigrationJob object
        """
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO migration_jobs (migration_id, job_id, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    migration_id,
                    job_id,
                    MigrationStatus.PENDING.value,
                    datetime.utcnow(),
                ),
            )

        logger.info(f"Created migration {migration_id}")
        return self.get_migration(migration_id)

    def get_migration(self, migration_id: str) -> Optional[MigrationJob]:
        """
        Get migration by ID

        Args:
            migration_id: Migration identifier

        Returns:
            MigrationJob object or None if not found
        """
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT migration_id, job_id, status, created_at, started_at,
                       completed_at, progress_percent, current_stage, error_message,
                       workbook_count, calculation_count, relationship_count
                FROM migration_jobs
                WHERE migration_id = ?
                """,
                (migration_id,),
            ).fetchone()

        if row:
            return MigrationJob.from_db_row(row)
        return None

    def update_migration_status(
        self,
        migration_id: str,
        status: MigrationStatus,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Update migration status

        Args:
            migration_id: Migration identifier
            status: New status
            error_message: Optional error message
        """
        now = datetime.utcnow()

        with get_db_connection() as conn:
            if status == MigrationStatus.PARSING:
                conn.execute(
                    """
                    UPDATE migration_jobs
                    SET status = ?, started_at = ?, error_message = NULL
                    WHERE migration_id = ?
                    """,
                    (status.value, now, migration_id),
                )
            elif status in [MigrationStatus.COMPLETED, MigrationStatus.FAILED]:
                conn.execute(
                    """
                    UPDATE migration_jobs
                    SET status = ?, completed_at = ?, error_message = ?
                    WHERE migration_id = ?
                    """,
                    (status.value, now, error_message, migration_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE migration_jobs
                    SET status = ?, error_message = ?
                    WHERE migration_id = ?
                    """,
                    (status.value, error_message, migration_id),
                )

        logger.info(f"Updated migration {migration_id} status to {status.value}")

    def update_migration_progress(
        self,
        migration_id: str,
        progress_percent: int,
        current_stage: str,
        message: Optional[str] = None,
    ) -> None:
        """
        Update migration progress

        Args:
            migration_id: Migration identifier
            progress_percent: Progress percentage (0-100)
            current_stage: Current stage name
            message: Optional progress message
        """
        with get_db_connection() as conn:
            # Update migration job
            conn.execute(
                """
                UPDATE migration_jobs
                SET progress_percent = ?, current_stage = ?
                WHERE migration_id = ?
                """,
                (progress_percent, current_stage, migration_id),
            )

            # Insert progress log
            conn.execute(
                """
                INSERT INTO migration_progress (migration_id, stage, message, percent, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (migration_id, current_stage, message, progress_percent, datetime.utcnow()),
            )

        logger.debug(
            f"Migration {migration_id}: {current_stage} ({progress_percent}%) - {message}"
        )

    def update_migration_counts(
        self,
        migration_id: str,
        workbook_count: Optional[int] = None,
        calculation_count: Optional[int] = None,
        relationship_count: Optional[int] = None,
    ) -> None:
        """
        Update migration counts

        Args:
            migration_id: Migration identifier
            workbook_count: Number of workbooks (optional)
            calculation_count: Number of calculations (optional)
            relationship_count: Number of relationships (optional)
        """
        updates = []
        params = []

        if workbook_count is not None:
            updates.append("workbook_count = ?")
            params.append(workbook_count)
        if calculation_count is not None:
            updates.append("calculation_count = ?")
            params.append(calculation_count)
        if relationship_count is not None:
            updates.append("relationship_count = ?")
            params.append(relationship_count)

        if not updates:
            return

        params.append(migration_id)
        query = f"UPDATE migration_jobs SET {', '.join(updates)} WHERE migration_id = ?"

        with get_db_connection() as conn:
            conn.execute(query, tuple(params))

    def delete_migration(self, migration_id: str) -> bool:
        """
        Delete migration (cascades to related tables)

        Args:
            migration_id: Migration identifier

        Returns:
            True if deleted, False if not found
        """
        with get_db_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM migration_jobs WHERE migration_id = ?", (migration_id,)
            )
            deleted = cursor.rowcount > 0

        if deleted:
            logger.info(f"Deleted migration {migration_id}")
        return deleted

    # ============================================
    # Tableau Workbook Operations
    # ============================================

    def save_workbook(self, workbook: TableauWorkbook) -> None:
        """
        Save Tableau workbook metadata

        Args:
            workbook: TableauWorkbook object
        """
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO tableau_workbooks
                (workbook_id, migration_id, filename, file_path, raw_model, worksheet_count, dashboard_count, data_source_count, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workbook.workbook_id,
                    workbook.migration_id,
                    workbook.filename,
                    workbook.file_path,
                    json.dumps(workbook.raw_model) if workbook.raw_model else None,
                    workbook.worksheet_count,
                    workbook.dashboard_count,
                    workbook.data_source_count,
                    datetime.utcnow(),
                ),
            )

        logger.debug(f"Saved workbook {workbook.workbook_id}")

    def get_workbooks_by_migration(self, migration_id: str) -> List[TableauWorkbook]:
        """
        Get all workbooks for a migration

        Args:
            migration_id: Migration identifier

        Returns:
            List of TableauWorkbook objects
        """
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT workbook_id, migration_id, filename, file_path, raw_model,
                       worksheet_count, dashboard_count, data_source_count, extracted_at
                FROM tableau_workbooks
                WHERE migration_id = ?
                ORDER BY extracted_at
                """,
                (migration_id,),
            ).fetchall()

        return [TableauWorkbook.from_db_row(row) for row in rows]

    # ============================================
    # Tableau Calculation Operations
    # ============================================

    def save_calculation(self, calculation: TableauCalculation) -> None:
        """
        Save Tableau calculation

        Args:
            calculation: TableauCalculation object
        """
        # Merge native fields into visual_context dict
        visual_context_dict = calculation.visual_context or {}
        visual_context_dict.update({
            "is_lod": calculation.is_lod,
            "is_table_calc": calculation.is_table_calc,
            "used_in_tooltips": calculation.used_in_tooltips,
            "used_in_filters": calculation.used_in_filters,
        })
        visual_context_json = json.dumps(visual_context_dict)
        
        used_in_str = (
            ",".join(calculation.used_in_worksheets)
            if calculation.used_in_worksheets
            else None
        )

        # Serialize dependency metadata (NEW)
        depends_on_json = (
            json.dumps(calculation.depends_on) if calculation.depends_on else None
        )

        depends_on_metadata_json = None
        if calculation.depends_on_metadata:
            # Convert FieldDependency objects to serializable dicts
            depends_on_metadata_dict = {
                field_name: {
                    "field_type": metadata.get("field_type"),
                    "original_role": metadata.get("original_role"),
                    "is_aggregated": metadata.get("is_aggregated"),
                }
                for field_name, metadata in calculation.depends_on_metadata.items()
            }
            depends_on_metadata_json = json.dumps(depends_on_metadata_dict)

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO tableau_calculations
                (calc_id, workbook_id, calc_name, calc_formula, calc_type, visual_context, dependency_level, used_in_worksheets, depends_on, depends_on_metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    calculation.calc_id,
                    calculation.workbook_id,
                    calculation.calc_name,
                    calculation.calc_formula,
                    calculation.calc_type.value,
                    visual_context_json,
                    calculation.dependency_level,
                    used_in_str,
                    depends_on_json,
                    depends_on_metadata_json,
                    datetime.utcnow(),
                ),
            )

        logger.debug(f"Saved calculation {calculation.calc_id}")

    def get_calculations_by_workbook(
        self, workbook_id: str
    ) -> List[TableauCalculation]:
        """
        Get all calculations for a workbook

        Args:
            workbook_id: Workbook identifier

        Returns:
            List of TableauCalculation objects
        """
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT calc_id, workbook_id, calc_name, calc_formula, calc_type,
                       visual_context, dependency_level, depends_on, depends_on_metadata, used_in_worksheets, created_at
                FROM tableau_calculations
                WHERE workbook_id = ?
                ORDER BY dependency_level, calc_name
                """,
                (workbook_id,),
            ).fetchall()

        return [TableauCalculation.from_db_row(row) for row in rows]

    def get_calculations_by_migration(
        self, migration_id: str
    ) -> List[TableauCalculation]:
        """
        Get all calculations for a migration

        Args:
            migration_id: Migration identifier

        Returns:
            List of TableauCalculation objects
        """
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT c.calc_id, c.workbook_id, c.calc_name, c.calc_formula, c.calc_type,
                       c.visual_context, c.dependency_level, c.depends_on, c.depends_on_metadata, c.used_in_worksheets, c.created_at
                FROM tableau_calculations c
                JOIN tableau_workbooks w ON c.workbook_id = w.workbook_id
                WHERE w.migration_id = ?
                ORDER BY c.dependency_level, c.calc_name
                """,
                (migration_id,),
            ).fetchall()

        return [TableauCalculation.from_db_row(row) for row in rows]

    # ============================================
    # DAX Conversion Operations
    # ============================================

    def save_conversion(self, conversion: DAXConversion) -> None:
        """
        Save DAX conversion result

        Args:
            conversion: DAXConversion object
        """
        warnings_json = json.dumps(conversion.warnings) if conversion.warnings else None

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO dax_conversions
                (conversion_id, calc_id, migration_id, dax_formula, conversion_method,
                 confidence_score, reasoning, warnings, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversion.conversion_id,
                    conversion.calc_id,
                    conversion.migration_id,
                    conversion.dax_formula,
                    conversion.conversion_method.value,
                    conversion.confidence_score,
                    conversion.reasoning,
                    warnings_json,
                    conversion.status.value,
                    datetime.utcnow(),
                    datetime.utcnow(),
                ),
            )

        logger.debug(f"Saved conversion {conversion.conversion_id}")

    def update_conversion(
        self,
        conversion_id: str,
        dax_formula: Optional[str] = None,
        status: Optional[ConversionStatus] = None,
        confidence_score: Optional[float] = None,
    ) -> None:
        """
        Update DAX conversion

        Args:
            conversion_id: Conversion identifier
            dax_formula: Updated DAX formula
            status: Updated status
            confidence_score: Updated confidence score
        """
        updates = ["updated_at = ?"]
        params = [datetime.utcnow()]

        if dax_formula is not None:
            updates.append("dax_formula = ?")
            params.append(dax_formula)
        if status is not None:
            updates.append("status = ?")
            params.append(status.value)
        if confidence_score is not None:
            updates.append("confidence_score = ?")
            params.append(confidence_score)

        params.append(conversion_id)
        query = f"UPDATE dax_conversions SET {', '.join(updates)} WHERE conversion_id = ?"

        with get_db_connection() as conn:
            conn.execute(query, tuple(params))

        logger.debug(f"Updated conversion {conversion_id}")

    def get_conversions_by_migration(self, migration_id: str) -> List[DAXConversion]:
        """
        Get all conversions for a migration

        Args:
            migration_id: Migration identifier

        Returns:
            List of DAXConversion objects
        """
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT conversion_id, calc_id, migration_id, dax_formula, conversion_method,
                       confidence_score, reasoning, warnings, status, created_at, updated_at
                FROM dax_conversions
                WHERE migration_id = ?
                ORDER BY created_at
                """,
                (migration_id,),
            ).fetchall()

        return [DAXConversion.from_db_row(row) for row in rows]

    def get_conversion(self, conversion_id: str) -> Optional[DAXConversion]:
        """
        Get conversion by ID

        Args:
            conversion_id: Conversion identifier

        Returns:
            DAXConversion object or None
        """
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT conversion_id, calc_id, migration_id, dax_formula, conversion_method,
                       confidence_score, reasoning, warnings, status, created_at, updated_at
                FROM dax_conversions
                WHERE conversion_id = ?
                """,
                (conversion_id,),
            ).fetchone()

        if row:
            return DAXConversion.from_db_row(row)
        return None

    # ============================================
    # Validation Result Operations
    # ============================================

    def save_validation_result(self, result: ValidationResult) -> None:
        """
        Save validation result

        Args:
            result: ValidationResult object
        """
        test_slice_json = json.dumps(result.test_slice) if result.test_slice else None

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO validation_results
                (validation_id, conversion_id, test_slice, tableau_value, dax_value,
                 delta, relative_error, passed, error_category, correction_attempts, validated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.validation_id,
                    result.conversion_id,
                    test_slice_json,
                    result.tableau_value,
                    result.dax_value,
                    result.delta,
                    result.relative_error,
                    result.passed,
                    result.error_category.value if result.error_category else None,
                    result.correction_attempts,
                    datetime.utcnow(),
                ),
            )

        logger.debug(f"Saved validation result {result.validation_id}")

    def get_validation_results_by_conversion(
        self, conversion_id: str
    ) -> List[ValidationResult]:
        """
        Get all validation results for a conversion

        Args:
            conversion_id: Conversion identifier

        Returns:
            List of ValidationResult objects
        """
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT validation_id, conversion_id, test_slice, tableau_value, dax_value,
                       delta, relative_error, passed, error_category, correction_attempts, validated_at
                FROM validation_results
                WHERE conversion_id = ?
                ORDER BY validated_at
                """,
                (conversion_id,),
            ).fetchall()

        return [ValidationResult.from_db_row(row) for row in rows]

    def get_validation_results_by_migration(
        self, migration_id: str
    ) -> Dict[str, List[ValidationResult]]:
        """
        Get ALL validation results for a migration in a single query (BULK FETCH)

        This fixes the N+1 query problem in the validation results endpoint.
        Instead of 1 + N queries, this uses just 1 JOIN query.

        Args:
            migration_id: Migration identifier

        Returns:
            Dictionary mapping conversion_id to list of ValidationResult objects
        """
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT vr.validation_id, vr.conversion_id, vr.test_slice, vr.tableau_value, vr.dax_value,
                       vr.delta, vr.relative_error, vr.passed, vr.error_category, vr.correction_attempts, vr.validated_at
                FROM validation_results vr
                JOIN dax_conversions dc ON vr.conversion_id = dc.conversion_id
                WHERE dc.migration_id = ?
                ORDER BY vr.conversion_id, vr.validated_at
                """,
                (migration_id,),
            ).fetchall()

        # Group by conversion_id
        results_by_conversion = {}
        for row in rows:
            conv_id = row[1]  # conversion_id is at index 1
            if conv_id not in results_by_conversion:
                results_by_conversion[conv_id] = []
            results_by_conversion[conv_id].append(ValidationResult.from_db_row(row))

        logger.debug(f"Bulk fetched validation results for {len(results_by_conversion)} conversions")
        return results_by_conversion

    def get_validation_summary(self, migration_id: str) -> Dict[str, Any]:
        """
        Get validation summary for a migration

        Args:
            migration_id: Migration identifier

        Returns:
            Dictionary with validation summary statistics
        """
        with get_db_connection() as conn:
            result = conn.execute(
                """
                SELECT
                    COUNT(*) as total_validations,
                    SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed_count,
                    SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) as failed_count,
                    AVG(delta) as avg_delta,
                    MAX(delta) as max_delta
                FROM validation_results vr
                JOIN dax_conversions dc ON vr.conversion_id = dc.conversion_id
                WHERE dc.migration_id = ?
                """,
                (migration_id,),
            ).fetchone()

        if result and result[0] > 0:
            return {
                "total_validations": result[0],
                "passed_count": result[1],
                "failed_count": result[2],
                "pass_rate": result[1] / result[0] if result[0] > 0 else 0,
                "avg_delta": result[3],
                "max_delta": result[4],
            }

        return {
            "total_validations": 0,
            "passed_count": 0,
            "failed_count": 0,
            "pass_rate": 0,
            "avg_delta": 0,
            "max_delta": 0,
        }
