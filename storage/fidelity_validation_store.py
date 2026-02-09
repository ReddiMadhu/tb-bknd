"""Fidelity Validation Store - Database operations for 100% fidelity validation"""
import json
import uuid
from typing import Dict, List, Any, Optional
from loguru import logger

from storage.database import get_db_connection


class FidelityValidationStore:
    """
    Store for high-fidelity validation results and self-healing corrections

    Stores:
    - Validation results from validate_conversion_v2()
    - Test slices with Tableau vs DAX comparisons
    - Self-healing agent correction attempts
    """

    def __init__(self):
        """Initialize the store"""
        logger.info("Fidelity Validation Store initialized")

    # ============================================
    # Validation Results
    # ============================================

    def save_validation_result(
        self,
        migration_id: str,
        conversion_id: str,
        validation_result: Any  # ValidationResult dataclass
    ) -> str:
        """
        Save validation result to database

        Args:
            migration_id: Migration ID
            conversion_id: Conversion ID
            validation_result: ValidationResult from validate_conversion_v2()

        Returns:
            validation_id
        """
        validation_id = f"val_{uuid.uuid4().hex[:12]}"

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Insert validation record
            cursor.execute("""
                INSERT INTO fidelity_validations (
                    validation_id,
                    migration_id,
                    conversion_id,
                    overall_passed,
                    pass_rate,
                    correction_attempts,
                    final_dax
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                validation_id,
                migration_id,
                conversion_id,
                validation_result.overall_passed,
                validation_result.pass_rate,
                validation_result.correction_attempts,
                validation_result.final_dax
            ))

            # Insert test slices
            for slice in validation_result.test_slices:
                slice_id = f"slice_{uuid.uuid4().hex[:12]}"

                cursor.execute("""
                    INSERT INTO validation_test_slices (
                        slice_id,
                        validation_id,
                        dimensions,
                        tableau_value,
                        dax_value,
                        delta,
                        relative_error,
                        passed,
                        error_category
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    slice_id,
                    validation_id,
                    json.dumps(slice.dimensions),
                    slice.tableau_value,
                    slice.dax_value,
                    slice.delta,
                    slice.relative_error,
                    slice.passed,
                    slice.error_category.value
                ))

            conn.commit()

        logger.info(f"✅ Saved validation result {validation_id} ({validation_result.pass_rate:.1%} pass rate)")
        return validation_id

    def get_validation_by_migration(self, migration_id: str) -> Optional[Dict[str, Any]]:
        """
        Get latest validation result for a migration

        Args:
            migration_id: Migration ID

        Returns:
            Validation result with test slices
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Get validation record
            cursor.execute("""
                SELECT validation_id, conversion_id, overall_passed, pass_rate,
                       correction_attempts, final_dax, created_at
                FROM fidelity_validations
                WHERE migration_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (migration_id,))

            row = cursor.fetchone()
            if not row:
                return None

            validation_id = row[0]

            # Get test slices
            cursor.execute("""
                SELECT dimensions, tableau_value, dax_value, delta,
                       relative_error, passed, error_category
                FROM validation_test_slices
                WHERE validation_id = ?
            """, (validation_id,))

            test_slices = []
            for slice_row in cursor.fetchall():
                test_slices.append({
                    "dimensions": json.loads(slice_row[0]),
                    "tableau_value": slice_row[1],
                    "dax_value": slice_row[2],
                    "delta": slice_row[3],
                    "relative_error": slice_row[4],
                    "passed": bool(slice_row[5]),
                    "error_category": slice_row[6]
                })

            return {
                "validation_id": validation_id,
                "conversion_id": row[1],
                "overall_passed": bool(row[2]),
                "pass_rate": row[3],
                "correction_attempts": row[4],
                "final_dax": row[5],
                "created_at": row[6],
                "test_slices": test_slices
            }

    def get_validation_by_conversion(self, conversion_id: str) -> Optional[Dict[str, Any]]:
        """
        Get validation result for a specific conversion

        Args:
            conversion_id: Conversion ID

        Returns:
            Validation result with test slices
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT validation_id, migration_id, overall_passed, pass_rate,
                       correction_attempts, final_dax, created_at
                FROM fidelity_validations
                WHERE conversion_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (conversion_id,))

            row = cursor.fetchone()
            if not row:
                return None

            validation_id = row[0]

            # Get test slices
            cursor.execute("""
                SELECT dimensions, tableau_value, dax_value, delta,
                       relative_error, passed, error_category
                FROM validation_test_slices
                WHERE validation_id = ?
            """, (validation_id,))

            test_slices = []
            for slice_row in cursor.fetchall():
                test_slices.append({
                    "dimensions": json.loads(slice_row[0]),
                    "tableau_value": slice_row[1],
                    "dax_value": slice_row[2],
                    "delta": slice_row[3],
                    "relative_error": slice_row[4],
                    "passed": bool(slice_row[5]),
                    "error_category": slice_row[6]
                })

            return {
                "validation_id": validation_id,
                "migration_id": row[1],
                "conversion_id": conversion_id,
                "overall_passed": bool(row[2]),
                "pass_rate": row[3],
                "correction_attempts": row[4],
                "final_dax": row[5],
                "created_at": row[6],
                "test_slices": test_slices
            }

    # ============================================
    # Correction Attempts
    # ============================================

    def save_correction_attempt(
        self,
        validation_id: str,
        attempt_number: int,
        original_dax: str,
        corrected_dax: str,
        root_cause: str,
        explanation: str,
        changes_made: List[str]
    ) -> str:
        """
        Save self-healing correction attempt

        Args:
            validation_id: Validation ID
            attempt_number: Attempt number (1, 2, 3)
            original_dax: Original DAX that failed
            corrected_dax: Corrected DAX
            root_cause: Root cause analysis
            explanation: AI explanation
            changes_made: List of changes

        Returns:
            attempt_id
        """
        attempt_id = f"attempt_{uuid.uuid4().hex[:12]}"

        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO correction_attempts (
                    attempt_id,
                    validation_id,
                    attempt_number,
                    original_dax,
                    corrected_dax,
                    root_cause,
                    explanation,
                    changes_made
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                attempt_id,
                validation_id,
                attempt_number,
                original_dax,
                corrected_dax,
                root_cause,
                explanation,
                json.dumps(changes_made)
            ))

            conn.commit()

        logger.info(f"✅ Saved correction attempt {attempt_number}")
        return attempt_id

    def get_correction_history(self, validation_id: str) -> List[Dict[str, Any]]:
        """
        Get all correction attempts for a validation

        Args:
            validation_id: Validation ID

        Returns:
            List of correction attempts
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT attempt_id, attempt_number, original_dax, corrected_dax,
                       root_cause, explanation, changes_made, created_at
                FROM correction_attempts
                WHERE validation_id = ?
                ORDER BY attempt_number
            """, (validation_id,))

            attempts = []
            for row in cursor.fetchall():
                attempts.append({
                    "attempt_id": row[0],
                    "attempt_number": row[1],
                    "original_dax": row[2],
                    "corrected_dax": row[3],
                    "root_cause": row[4],
                    "explanation": row[5],
                    "changes_made": json.loads(row[6]),
                    "created_at": row[7]
                })

            return attempts

    def get_correction_history_by_migration(self, migration_id: str) -> List[Dict[str, Any]]:
        """
        Get all correction attempts for a migration

        Args:
            migration_id: Migration ID

        Returns:
            List of all correction attempts across all validations
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT ca.attempt_id, ca.attempt_number, ca.original_dax, ca.corrected_dax,
                       ca.root_cause, ca.explanation, ca.changes_made, ca.created_at,
                       fv.conversion_id
                FROM correction_attempts ca
                JOIN fidelity_validations fv ON ca.validation_id = fv.validation_id
                WHERE fv.migration_id = ?
                ORDER BY ca.created_at
            """, (migration_id,))

            attempts = []
            for row in cursor.fetchall():
                attempts.append({
                    "attempt_id": row[0],
                    "attempt_number": row[1],
                    "original_dax": row[2],
                    "corrected_dax": row[3],
                    "root_cause": row[4],
                    "explanation": row[5],
                    "changes_made": json.loads(row[6]),
                    "created_at": row[7],
                    "conversion_id": row[8]
                })

            return attempts

    # ============================================
    # Statistics
    # ============================================

    def get_validation_stats(self, migration_id: str) -> Dict[str, Any]:
        """
        Get validation statistics for a migration

        Args:
            migration_id: Migration ID

        Returns:
            Statistics summary
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Get overall stats
            cursor.execute("""
                SELECT
                    COUNT(*) as total_validations,
                    AVG(pass_rate) as avg_pass_rate,
                    SUM(CASE WHEN overall_passed THEN 1 ELSE 0 END) as perfect_matches,
                    SUM(correction_attempts) as total_corrections
                FROM fidelity_validations
                WHERE migration_id = ?
            """, (migration_id,))

            row = cursor.fetchone()

            # Get error category breakdown
            cursor.execute("""
                SELECT error_category, COUNT(*) as count
                FROM validation_test_slices vts
                JOIN fidelity_validations fv ON vts.validation_id = fv.validation_id
                WHERE fv.migration_id = ? AND vts.passed = 0
                GROUP BY error_category
            """, (migration_id,))

            error_breakdown = {row[0]: row[1] for row in cursor.fetchall()}

            return {
                "total_validations": row[0] or 0,
                "avg_pass_rate": row[1] or 0,
                "perfect_matches": row[2] or 0,
                "total_corrections": row[3] or 0,
                "error_breakdown": error_breakdown
            }
