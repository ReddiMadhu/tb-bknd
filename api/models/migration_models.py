"""Domain models for Tableau migration"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum


class MigrationStatus(str, Enum):
    """Migration job status"""
    PENDING = "pending"
    PARSING = "parsing"
    DISCOVERING = "discovering"
    CONVERTING = "converting"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"


class CalculationType(str, Enum):
    """Tableau calculation type"""
    CALCULATED_FIELD = "CALCULATED_FIELD"
    MEASURE = "MEASURE"
    PARAMETER = "PARAMETER"
    LOD = "LOD"
    TABLE_CALC = "TABLE_CALC"


class ConversionMethod(str, Enum):
    """DAX conversion method"""
    LLM_PATTERN = "LLM_PATTERN"
    LLM_GENERATED = "LLM_GENERATED"
    RULE_BASED = "RULE_BASED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"


class ConversionStatus(str, Enum):
    """DAX conversion status"""
    PENDING = "pending"
    VALIDATED = "validated"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class ErrorCategory(str, Enum):
    """Validation error categories"""
    PERFECT_MATCH = "PERFECT_MATCH"
    ROUNDING_ERROR = "ROUNDING_ERROR"
    NULL_HANDLING = "NULL_HANDLING"
    CONTEXT_SHIFT = "CONTEXT_SHIFT"
    SCALE_ERROR = "SCALE_ERROR"
    AGGREGATION_MISMATCH = "AGGREGATION_MISMATCH"
    MISSING_VALUE = "MISSING_VALUE"


@dataclass
class MigrationJob:
    """Migration job domain model"""
    migration_id: str
    status: MigrationStatus
    created_at: datetime
    workbook_count: int = 0
    calculation_count: int = 0
    relationship_count: int = 0
    progress_percent: int = 0
    current_stage: Optional[str] = None
    job_id: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None

    @classmethod
    def from_db_row(cls, row) -> "MigrationJob":
        """Create MigrationJob from database row"""
        if row is None:
            return None

        return cls(
            migration_id=row["migration_id"] if isinstance(row, dict) else row[0],
            job_id=row["job_id"] if isinstance(row, dict) else row[1],
            status=MigrationStatus(row["status"] if isinstance(row, dict) else row[2]),
            created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"] if isinstance(row, dict) else row[3], str) else (row["created_at"] if isinstance(row, dict) else row[3]),
            started_at=datetime.fromisoformat(row["started_at"]) if (row["started_at"] if isinstance(row, dict) else row[4]) and isinstance((row["started_at"] if isinstance(row, dict) else row[4]), str) else (row["started_at"] if isinstance(row, dict) else row[4]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if (row["completed_at"] if isinstance(row, dict) else row[5]) and isinstance((row["completed_at"] if isinstance(row, dict) else row[5]), str) else (row["completed_at"] if isinstance(row, dict) else row[5]),
            progress_percent=row["progress_percent"] if isinstance(row, dict) else (row[6] or 0),
            current_stage=row["current_stage"] if isinstance(row, dict) else row[7],
            error_message=row["error_message"] if isinstance(row, dict) else row[8],
            workbook_count=row["workbook_count"] if isinstance(row, dict) else (row[9] or 0),
            calculation_count=row["calculation_count"] if isinstance(row, dict) else (row[10] or 0),
            relationship_count=row["relationship_count"] if isinstance(row, dict) else (row[11] or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "migration_id": self.migration_id,
            "job_id": self.job_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "progress_percent": self.progress_percent,
            "current_stage": self.current_stage,
            "error_message": self.error_message,
            "workbook_count": self.workbook_count,
            "calculation_count": self.calculation_count,
            "relationship_count": self.relationship_count,
        }


@dataclass
class TableauWorkbook:
    """Tableau workbook metadata"""
    workbook_id: str
    migration_id: str
    filename: str
    worksheet_count: int = 0
    dashboard_count: int = 0
    data_source_count: int = 0
    file_path: Optional[str] = None
    extracted_at: Optional[datetime] = None

    @classmethod
    def from_db_row(cls, row) -> "TableauWorkbook":
        """Create TableauWorkbook from database row"""
        if row is None:
            return None

        return cls(
            workbook_id=row["workbook_id"] if isinstance(row, dict) else row[0],
            migration_id=row["migration_id"] if isinstance(row, dict) else row[1],
            filename=row["filename"] if isinstance(row, dict) else row[2],
            file_path=row["file_path"] if isinstance(row, dict) else row[3],
            worksheet_count=row["worksheet_count"] if isinstance(row, dict) else (row[4] or 0),
            dashboard_count=row["dashboard_count"] if isinstance(row, dict) else (row[5] or 0),
            data_source_count=row["data_source_count"] if isinstance(row, dict) else (row[6] or 0),
            extracted_at=datetime.fromisoformat(row["extracted_at"]) if (row["extracted_at"] if isinstance(row, dict) else row[7]) and isinstance((row["extracted_at"] if isinstance(row, dict) else row[7]), str) else (row["extracted_at"] if isinstance(row, dict) else row[7]),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "workbook_id": self.workbook_id,
            "migration_id": self.migration_id,
            "filename": self.filename,
            "file_path": self.file_path,
            "worksheet_count": self.worksheet_count,
            "dashboard_count": self.dashboard_count,
            "data_source_count": self.data_source_count,
            "extracted_at": self.extracted_at.isoformat() if self.extracted_at else None,
        }


@dataclass
class TableauCalculation:
    """Tableau calculation metadata"""
    calc_id: str
    workbook_id: str
    calc_name: str
    calc_formula: str
    calc_type: CalculationType
    dependency_level: int = 0
    visual_context: Optional[Dict[str, Any]] = None
    used_in_worksheets: Optional[List[str]] = None
    depends_on: Optional[List[str]] = None
    depends_on_metadata: Optional[Dict[str, Dict[str, Any]]] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_db_row(cls, row) -> "TableauCalculation":
        """Create TableauCalculation from database row"""
        if row is None:
            return None

        import json
        visual_context_str = row["visual_context"] if isinstance(row, dict) else row[5]
        used_in_str = row["used_in_worksheets"] if isinstance(row, dict) else row[6]
        depends_on_str = row["depends_on"] if isinstance(row, dict) else row[8]
        depends_on_metadata_str = row["depends_on_metadata"] if isinstance(row, dict) else row[9]

        return cls(
            calc_id=row["calc_id"] if isinstance(row, dict) else row[0],
            workbook_id=row["workbook_id"] if isinstance(row, dict) else row[1],
            calc_name=row["calc_name"] if isinstance(row, dict) else row[2],
            calc_formula=row["calc_formula"] if isinstance(row, dict) else row[3],
            calc_type=CalculationType(row["calc_type"] if isinstance(row, dict) else row[4]),
            visual_context=json.loads(visual_context_str) if visual_context_str else None,
            dependency_level=row["dependency_level"] if isinstance(row, dict) else (row[7] or 0),
            used_in_worksheets=used_in_str.split(",") if used_in_str else None,
            depends_on=json.loads(depends_on_str) if depends_on_str else None,
            depends_on_metadata=json.loads(depends_on_metadata_str) if depends_on_metadata_str else None,
            created_at=datetime.fromisoformat(row["created_at"]) if (row["created_at"] if isinstance(row, dict) else row[10]) and isinstance((row["created_at"] if isinstance(row, dict) else row[10]), str) else (row["created_at"] if isinstance(row, dict) else row[10]),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "calc_id": self.calc_id,
            "workbook_id": self.workbook_id,
            "calc_name": self.calc_name,
            "calc_formula": self.calc_formula,
            "calc_type": self.calc_type.value,
            "visual_context": self.visual_context,
            "dependency_level": self.dependency_level,
            "used_in_worksheets": self.used_in_worksheets,
            "depends_on": self.depends_on,
            "depends_on_metadata": self.depends_on_metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass
class DAXConversion:
    """DAX conversion result"""
    conversion_id: str
    calc_id: str
    migration_id: str
    dax_formula: str
    conversion_method: ConversionMethod
    status: ConversionStatus
    confidence_score: Optional[float] = None
    reasoning: Optional[str] = None
    warnings: Optional[List[str]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_db_row(cls, row) -> "DAXConversion":
        """Create DAXConversion from database row"""
        if row is None:
            return None

        import json
        warnings_str = row["warnings"] if isinstance(row, dict) else row[7]

        return cls(
            conversion_id=row["conversion_id"] if isinstance(row, dict) else row[0],
            calc_id=row["calc_id"] if isinstance(row, dict) else row[1],
            migration_id=row["migration_id"] if isinstance(row, dict) else row[2],
            dax_formula=row["dax_formula"] if isinstance(row, dict) else row[3],
            conversion_method=ConversionMethod(row["conversion_method"] if isinstance(row, dict) else row[4]),
            confidence_score=row["confidence_score"] if isinstance(row, dict) else row[5],
            reasoning=row["reasoning"] if isinstance(row, dict) else row[6],
            warnings=json.loads(warnings_str) if warnings_str else None,
            status=ConversionStatus(row["status"] if isinstance(row, dict) else row[8]),
            created_at=datetime.fromisoformat(row["created_at"]) if (row["created_at"] if isinstance(row, dict) else row[9]) and isinstance((row["created_at"] if isinstance(row, dict) else row[9]), str) else (row["created_at"] if isinstance(row, dict) else row[9]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if (row["updated_at"] if isinstance(row, dict) else row[10]) and isinstance((row["updated_at"] if isinstance(row, dict) else row[10]), str) else (row["updated_at"] if isinstance(row, dict) else row[10]),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "conversion_id": self.conversion_id,
            "calc_id": self.calc_id,
            "migration_id": self.migration_id,
            "dax_formula": self.dax_formula,
            "conversion_method": self.conversion_method.value,
            "confidence_score": self.confidence_score,
            "reasoning": self.reasoning,
            "warnings": self.warnings,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class ValidationResult:
    """Validation result"""
    validation_id: str
    conversion_id: str
    tableau_value: Optional[float]
    dax_value: Optional[float]
    delta: float
    passed: bool
    test_slice: Optional[Dict[str, Any]] = None
    relative_error: Optional[float] = None
    error_category: Optional[ErrorCategory] = None
    correction_attempts: int = 0
    validated_at: Optional[datetime] = None

    @classmethod
    def from_db_row(cls, row) -> "ValidationResult":
        """Create ValidationResult from database row"""
        if row is None:
            return None

        import json
        test_slice_str = row["test_slice"] if isinstance(row, dict) else row[2]

        return cls(
            validation_id=row["validation_id"] if isinstance(row, dict) else row[0],
            conversion_id=row["conversion_id"] if isinstance(row, dict) else row[1],
            test_slice=json.loads(test_slice_str) if test_slice_str else None,
            tableau_value=row["tableau_value"] if isinstance(row, dict) else row[3],
            dax_value=row["dax_value"] if isinstance(row, dict) else row[4],
            delta=row["delta"] if isinstance(row, dict) else row[5],
            relative_error=row["relative_error"] if isinstance(row, dict) else row[6],
            passed=bool(row["passed"] if isinstance(row, dict) else row[7]),
            error_category=ErrorCategory(row["error_category"]) if (row["error_category"] if isinstance(row, dict) else row[8]) else None,
            correction_attempts=row["correction_attempts"] if isinstance(row, dict) else (row[9] or 0),
            validated_at=datetime.fromisoformat(row["validated_at"]) if (row["validated_at"] if isinstance(row, dict) else row[10]) and isinstance((row["validated_at"] if isinstance(row, dict) else row[10]), str) else (row["validated_at"] if isinstance(row, dict) else row[10]),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "validation_id": self.validation_id,
            "conversion_id": self.conversion_id,
            "test_slice": self.test_slice,
            "tableau_value": self.tableau_value,
            "dax_value": self.dax_value,
            "delta": self.delta,
            "relative_error": self.relative_error,
            "passed": self.passed,
            "error_category": self.error_category.value if self.error_category else None,
            "correction_attempts": self.correction_attempts,
            "validated_at": self.validated_at.isoformat() if self.validated_at else None,
        }
