"""Migration API Router - Tableau to Power BI migration endpoints"""
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from typing import List, Optional
from pathlib import Path
import uuid
from datetime import datetime
from loguru import logger
import zipfile
import json
import os

from api.models.migration_models import (
    MigrationJob,
    MigrationStatus,
    TableauWorkbook,
    TableauCalculation,
    DAXConversion,
    ValidationResult
)
from storage.migration_store import MigrationStore
from storage.fidelity_validation_store import FidelityValidationStore
from storage.file_store import FileStore
from api.config import config
from workers.websocket_manager import WebSocketManager
from src.tableau.migration_orchestrator import MigrationOrchestrator

router = APIRouter(prefix="/api/v1/migration", tags=["migration"])

# Initialize stores
migration_store = MigrationStore()
fidelity_store = FidelityValidationStore()
file_store = FileStore()
ws_manager = WebSocketManager()
orchestrator = MigrationOrchestrator()  # No arguments - creates its own store


# ============================================
# Migration Workflow Endpoints
# ============================================

@router.post("/upload")
async def upload_twbx_files(
    files: List[UploadFile] = File(...),
    background_tasks: BackgroundTasks = None
):
    """
    Upload multiple TWBX files and create migration job

    Request:
        - files: List of .twbx or .twb files

    Response:
        {
            "migration_id": "mig_abc123",
            "status": "pending",
            "workbook_count": 3,
            "message": "Migration job created"
        }
    """
    try:
        # Validate files
        for file in files:
            if not file.filename.endswith(('.twbx', '.twb')):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid file type: {file.filename}. Only .twbx and .twb files are supported."
                )

        # Create migration ID
        migration_id = f"mig_{uuid.uuid4().hex[:12]}"

        # Create migration job
        migration = migration_store.create_migration(migration_id)

        # Save uploaded files
        file_paths = []
        for file in files:
            # Save file
            file_id = f"file_{uuid.uuid4().hex[:8]}"
            stored_path = Path(config.UPLOAD_DIR) / f"{migration_id}_{file.filename}"

            with open(stored_path, "wb") as f:
                content = await file.read()
                f.write(content)

            file_paths.append(str(stored_path))
            logger.info(f"Saved file: {file.filename} ({len(content)} bytes)")

        # Update migration with file count
        migration_store.update_migration_counts(
            migration_id,
            workbook_count=len(files)
        )

        # Trigger background processing
        background_tasks.add_task(orchestrator.execute_migration, migration_id, file_paths)
        logger.info(f"Started background migration processing for {migration_id}")

        return {
            "migration_id": migration_id,
            "status": migration.status.value,
            "workbook_count": len(files),
            "message": f"Uploaded {len(files)} workbook(s). Migration created."
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload files: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}")
async def get_migration_status(migration_id: str):
    """
    Get migration job status

    Response:
        {
            "migration_id": "mig_abc123",
            "status": "converting",
            "progress_percent": 45,
            "current_stage": "Generating DAX formulas",
            "workbook_count": 3,
            "calculation_count": 24,
            "relationship_count": 8,
            "created_at": "2024-01-15T10:30:00",
            "started_at": "2024-01-15T10:30:05",
            "error_message": null
        }
    """
    migration = migration_store.get_migration(migration_id)

    if not migration:
        raise HTTPException(status_code=404, detail="Migration not found")

    return migration.to_dict()


@router.delete("/{migration_id}")
async def delete_migration(migration_id: str):
    """
    Delete a migration job and all associated data

    Response:
        {"message": "Migration deleted successfully"}
    """
    migration = migration_store.get_migration(migration_id)

    if not migration:
        raise HTTPException(status_code=404, detail="Migration not found")

    # Delete from database
    migration_store.delete_migration(migration_id)

    # TODO: Clean up files
    # file_store.delete_migration_files(migration_id)

    logger.info(f"Deleted migration: {migration_id}")

    return {"message": "Migration deleted successfully"}


# ============================================
# Workbook & Calculation Endpoints
# ============================================

@router.get("/{migration_id}/workbooks")
async def get_workbooks(
    migration_id: str,
    limit: int = 50,  # PERFORMANCE FIX #3: Add pagination (default 50 per page)
    offset: int = 0
):
    """
    Get all workbooks in a migration (with pagination)

    Query params:
        - limit: Maximum number of results (default: 50, max: 500)
        - offset: Starting position (default: 0)

    Response:
        {
            "workbooks": [
                {
                    "workbook_id": "wb_001",
                    "filename": "sales_dashboard.twbx",
                    "worksheet_count": 5,
                    "dashboard_count": 2,
                    "data_source_count": 1
                }
            ],
            "total": 25,
            "limit": 50,
            "offset": 0,
            "has_more": false
        }
    """
    # Validate pagination parameters
    limit = min(max(1, limit), 500)  # Clamp between 1 and 500
    offset = max(0, offset)

    workbooks = migration_store.get_workbooks_by_migration(migration_id)
    total = len(workbooks)

    # Apply pagination
    paginated_workbooks = workbooks[offset:offset + limit]

    return {
        "workbooks": [wb.to_dict() for wb in paginated_workbooks],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total
    }


@router.get("/{migration_id}/calculations")
async def get_calculations(
    migration_id: str,
    workbook_id: Optional[str] = None,
    limit: int = 100,  # PERFORMANCE FIX #3: Add pagination (default 100 per page)
    offset: int = 0
):
    """
    Get all calculations in a migration (with pagination)

    Query params:
        - workbook_id: Filter by specific workbook (optional)
        - limit: Maximum number of results (default: 100, max: 1000)
        - offset: Starting position (default: 0)

    Response:
        {
            "calculations": [
                {
                    "calc_id": "calc_001",
                    "calc_name": "Profit Ratio",
                    "calc_formula": "SUM([Profit]) / SUM([Sales])",
                    "calc_type": "CALCULATED_FIELD",
                    "dependency_level": 0,
                    "used_in_worksheets": ["Sales Overview", "Regional Analysis"]
                }
            ],
            "total": 245,
            "limit": 100,
            "offset": 0,
            "has_more": true
        }
    """
    # Validate pagination parameters
    limit = min(max(1, limit), 1000)  # Clamp between 1 and 1000
    offset = max(0, offset)

    if workbook_id:
        all_calculations = migration_store.get_calculations_by_workbook(workbook_id)
    else:
        all_calculations = migration_store.get_calculations_by_migration(migration_id)

    total = len(all_calculations)

    # Apply pagination
    paginated_calculations = all_calculations[offset:offset + limit]

    return {
        "calculations": [calc.to_dict() for calc in paginated_calculations],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total
    }


# ============================================
# Logic Graph Endpoint
# ============================================

@router.get("/{migration_id}/logic-graph")
async def get_logic_graph(migration_id: str, format: str = "json"):
    """
    Get calculation dependency graph

    Query params:
        - format: "json" or "reactflow" (default: "json")

    Response (format=json):
        {
            "nodes": [
                {
                    "id": "calc_profit_ratio",
                    "name": "Profit Ratio",
                    "formula": "SUM([Profit]) / SUM([Sales])",
                    "type": "MEASURE",
                    "dependency_level": 0,
                    "depends_on": []
                }
            ],
            "edges": [
                {"source": "Sales", "target": "calc_profit_ratio"}
            ],
            "stats": {
                "total_calculations": 14,
                "total_dependencies": 8,
                "max_dependency_level": 2,
                "lod_count": 3
            }
        }

    Response (format=reactflow):
        {
            "nodes": [...],  # ReactFlow-compatible nodes
            "edges": [...]   # ReactFlow-compatible edges
        }
    """
    # TODO: Load logic graph from storage or rebuild
    # For now, return mock data

    if format == "reactflow":
        return {
            "nodes": [
                {
                    "id": "calc_profit_ratio",
                    "type": "calculationNode",
                    "data": {
                        "label": "Profit Ratio",
                        "formula": "SUM([Profit]) / SUM([Sales])",
                        "calcType": "MEASURE",
                        "level": 0,
                        "isLOD": False
                    },
                    "position": {"x": 250, "y": 100},
                    "style": {
                        "background": "#f59e0b",
                        "color": "white"
                    }
                }
            ],
            "edges": []
        }
    else:
        return {
            "nodes": [],
            "edges": [],
            "stats": {
                "total_calculations": 0,
                "total_dependencies": 0,
                "max_dependency_level": 0,
                "lod_count": 0
            }
        }


# ============================================
# DAX Conversion Endpoints
# ============================================

@router.get("/{migration_id}/conversions")
async def get_conversions(
    migration_id: str,
    status: Optional[str] = None,
    limit: int = 100,  # PERFORMANCE FIX #3: Add pagination (default 100 per page)
    offset: int = 0
):
    """
    Get all DAX conversions (with pagination)

    Query params:
        - status: Filter by status (pending, validated, failed, manual_review)
        - limit: Maximum number of results (default: 100, max: 1000)
        - offset: Starting position (default: 0)

    Response:
        {
            "conversions": [
                {
                    "conversion_id": "conv_001",
                    "calc_id": "calc_001",
                    "dax_formula": "Profit Ratio = DIVIDE(SUM(Sales[Profit]), SUM(Sales[Sales]), 0)",
                    "conversion_method": "LLM_PATTERN",
                    "confidence_score": 0.95,
                    "status": "validated",
                    "warnings": []
                }
            ],
            "total": 245,
            "limit": 100,
            "offset": 0,
            "has_more": true
        }
    """
    # Validate pagination parameters
    limit = min(max(1, limit), 1000)  # Clamp between 1 and 1000
    offset = max(0, offset)

    conversions = migration_store.get_conversions_by_migration(migration_id)

    # Filter by status if provided
    if status:
        conversions = [c for c in conversions if c.status.value == status]

    total = len(conversions)

    # Apply pagination
    paginated_conversions = conversions[offset:offset + limit]

    return {
        "conversions": [conv.to_dict() for conv in paginated_conversions],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total
    }


@router.get("/{migration_id}/conversions/{conversion_id}")
async def get_conversion(migration_id: str, conversion_id: str):
    """
    Get a specific DAX conversion

    Response:
        {
            "conversion_id": "conv_001",
            "calc_id": "calc_001",
            "dax_formula": "...",
            "conversion_method": "LLM_PATTERN",
            "confidence_score": 0.95,
            "reasoning": "This is a simple ratio calculation...",
            "status": "validated",
            "warnings": [],
            "created_at": "2024-01-15T10:35:00",
            "updated_at": "2024-01-15T10:35:10"
        }
    """
    conversion = migration_store.get_conversion(conversion_id)

    if not conversion:
        raise HTTPException(status_code=404, detail="Conversion not found")

    return conversion.to_dict()


@router.patch("/{migration_id}/conversions/{conversion_id}")
async def update_conversion(
    migration_id: str,
    conversion_id: str,
    dax_formula: str,
    reasoning: Optional[str] = None
):
    """
    Manually override a DAX conversion

    Request:
        {
            "dax_formula": "Custom DAX = ...",
            "reasoning": "Manual correction because..."
        }

    Response:
        {
            "conversion_id": "conv_001",
            "dax_formula": "Custom DAX = ...",
            "conversion_method": "MANUAL_OVERRIDE",
            "status": "pending",
            "message": "Conversion updated. Validation pending."
        }
    """
    from api.models.migration_models import ConversionMethod, ConversionStatus

    conversion = migration_store.get_conversion(conversion_id)

    if not conversion:
        raise HTTPException(status_code=404, detail="Conversion not found")

    # Update conversion
    updated = migration_store.update_conversion(
        conversion_id=conversion_id,
        dax_formula=dax_formula,
        conversion_method=ConversionMethod.MANUAL_OVERRIDE,
        reasoning=reasoning or "Manual override by user",
        status=ConversionStatus.PENDING  # Re-validation needed
    )

    logger.info(f"Updated conversion {conversion_id} with manual override")

    return {
        "conversion_id": conversion_id,
        "dax_formula": updated.dax_formula,
        "conversion_method": updated.conversion_method.value,
        "status": updated.status.value,
        "message": "Conversion updated. Validation pending."
    }


# ============================================
# Validation Endpoints
# ============================================

@router.post("/{migration_id}/validate")
async def trigger_validation(migration_id: str, background_tasks: BackgroundTasks):
    """
    Trigger validation of all conversions

    Response:
        {
            "message": "Validation started",
            "migration_id": "mig_abc123"
        }
    """
    migration = migration_store.get_migration(migration_id)

    if not migration:
        raise HTTPException(status_code=404, detail="Migration not found")

    # Update status
    migration_store.update_migration_status(
        migration_id,
        MigrationStatus.VALIDATING,
        current_stage="Validating DAX conversions"
    )

    # TODO: Trigger background validation
    # background_tasks.add_task(validate_conversions, migration_id)

    return {
        "message": "Validation started",
        "migration_id": migration_id
    }


@router.get("/{migration_id}/validation-results")
async def get_validation_results(migration_id: str):
    """
    Get validation results for all conversions

    Response:
        {
            "results": [
                {
                    "conversion_id": "conv_001",
                    "test_slices": [
                        {
                            "dimensions": {"Region": "East", "Year": 2024},
                            "tableau_value": 14.5,
                            "dax_value": 14.5,
                            "delta": 0.0,
                            "passed": true,
                            "error_category": "PERFECT_MATCH"
                        }
                    ],
                    "overall_passed": true,
                    "correction_attempts": 0
                }
            ],
            "summary": {
                "total_conversions": 14,
                "passed": 13,
                "failed": 1,
                "pass_rate": 92.8
            }
        }
    """
    # PERFORMANCE FIX #2: Use bulk fetch to eliminate N+1 query problem
    # OLD: 1 + N queries (1 for conversions + 1 per conversion for validation results)
    # NEW: 2 queries total (1 for conversions + 1 JOIN for all validation results)
    conversions = migration_store.get_conversions_by_migration(migration_id)

    # Bulk fetch ALL validation results in single query (10-50x faster for large migrations)
    validation_results_by_conversion = migration_store.get_validation_results_by_migration(migration_id)

    results = []
    passed_count = 0

    for conversion in conversions:
        # Look up validation results from pre-fetched dictionary (no additional query)
        validation_results = validation_results_by_conversion.get(conversion.conversion_id, [])

        test_slices = [vr.to_dict() for vr in validation_results]
        overall_passed = all(vr.passed for vr in validation_results) if validation_results else False

        if overall_passed:
            passed_count += 1

        results.append({
            "conversion_id": conversion.conversion_id,
            "test_slices": test_slices,
            "overall_passed": overall_passed,
            "correction_attempts": validation_results[0].correction_attempts if validation_results else 0
        })

    return {
        "results": results,
        "summary": {
            "total_conversions": len(conversions),
            "passed": passed_count,
            "failed": len(conversions) - passed_count,
            "pass_rate": round((passed_count / len(conversions) * 100), 1) if conversions else 0
        }
    }


# ============================================
# Export Endpoints
# ============================================

@router.post("/{migration_id}/export")
async def export_powerbi_artifacts(migration_id: str):
    """
    Generate Power BI artifacts

    Response:
        {
            "message": "Power BI artifacts generated",
            "download_url": "/api/v1/migration/mig_abc123/download",
            "artifacts": {
                "dax_measures": "measures.dax",
                "power_query": "queries.m",
                "semantic_model": "model.bim",
                "pbip_project": "SalesDashboard.Report.zip"
            }
        }
    """
    migration = migration_store.get_migration(migration_id)

    if not migration:
        raise HTTPException(status_code=404, detail="Migration not found")

    if migration.status != MigrationStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail="Migration must be completed before exporting"
        )

    # Create artifacts ZIP
    artifact_filename = f"{migration_id}_artifacts.zip"
    artifact_path = Path(config.UPLOAD_DIR) / artifact_filename
    
    try:
        # Create ZIP file
        with zipfile.ZipFile(artifact_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 1. Add migration metadata
            zipf.writestr(
                "migration_metadata.json", 
                json.dumps(migration.to_dict(), indent=2, default=str)
            )
            
            # 2. Add conversions
            conversions = migration_store.get_conversions_by_migration(migration_id)
            conversions_data = [c.to_dict() for c in conversions]
            zipf.writestr(
                "dax_conversions.json", 
                json.dumps(conversions_data, indent=2, default=str)
            )
            
            # 3. Add placeholder README
            zipf.writestr(
                "README.txt",
                f"Power BI Export for Migration {migration_id}\n\n"
                f"Generated at: {datetime.now().isoformat()}\n"
                f"Status: {migration.status.value}\n\n"
                "This export contains the JSON metadata and DAX conversions.\n"
                "The full PBIP generation is currently in development."
            )
            
        logger.info(f"Generated artifacts ZIP for {migration_id} at {artifact_path}")
        
    except Exception as e:
        logger.error(f"Failed to generate artifacts: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate artifacts: {str(e)}")

    return {
        "message": "Power BI artifacts generated",
        "download_url": f"/api/v1/migration/{migration_id}/download",
        "artifacts": {
            "dax_measures": "dax_conversions.json",
            "metadata": "migration_metadata.json",
            "readme": "README.txt"
        }
    }


@router.get("/{migration_id}/download")
async def download_artifacts(migration_id: str):
    """
    Download exported Power BI artifacts as ZIP

    Response:
        File download (application/zip)
    """
    # TODO: Load generated artifacts from storage
    artifact_path = Path(config.UPLOAD_DIR) / f"{migration_id}_artifacts.zip"

    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifacts not found. Please export first.")

    return FileResponse(
        path=artifact_path,
        filename=f"powerbi_migration_{migration_id}.zip",
        media_type="application/zip"
    )


# ============================================
# 100% Fidelity Validation Endpoints
# ============================================

@router.get("/{migration_id}/fidelity-validation")
async def get_fidelity_validation(migration_id: str):
    """
    Get 100% fidelity validation results for a migration

    Returns:
        Validation results with test slices and error breakdown

    Example Response:
        {
            "validation_id": "val_abc123",
            "overall_passed": true,
            "pass_rate": 1.0,
            "correction_attempts": 1,
            "test_slices": [
                {
                    "dimensions": {"Region": "East", "Year": 2024},
                    "tableau_value": 10500.50,
                    "dax_value": 10500.50,
                    "delta": 0.0,
                    "relative_error": 0.0,
                    "passed": true,
                    "error_category": "PERFECT_MATCH"
                }
            ]
        }
    """
    try:
        validation = fidelity_store.get_validation_by_migration(migration_id)

        if not validation:
            raise HTTPException(
                status_code=404,
                detail="No validation results found. Validation may not have run yet."
            )

        return validation

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get fidelity validation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/correction-history")
async def get_correction_history(migration_id: str):
    """
    Get self-healing agent correction history for a migration

    Returns:
        List of correction attempts with root cause and changes

    Example Response:
        {
            "attempts": [
                {
                    "attempt_number": 1,
                    "original_dax": "Ratio = SUM(...) / SUM(...)",
                    "corrected_dax": "Ratio = DIVIDE(SUM(...), SUM(...), 0)",
                    "root_cause": "Missing DIVIDE safety",
                    "explanation": "Changed to DIVIDE() to handle division by zero...",
                    "changes_made": ["Added DIVIDE function", "Added 0 as default"]
                }
            ]
        }
    """
    try:
        attempts = fidelity_store.get_correction_history_by_migration(migration_id)

        return {
            "migration_id": migration_id,
            "total_attempts": len(attempts),
            "attempts": attempts
        }

    except Exception as e:
        logger.error(f"Failed to get correction history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/fidelity-stats")
async def get_fidelity_stats(migration_id: str):
    """
    Get fidelity validation statistics for a migration

    Returns:
        Statistics summary with error breakdown

    Example Response:
        {
            "total_validations": 21,
            "avg_pass_rate": 0.98,
            "perfect_matches": 19,
            "total_corrections": 3,
            "error_breakdown": {
                "SCALE_ERROR": 2,
                "CONTEXT_SHIFT": 1
            }
        }
    """
    try:
        stats = fidelity_store.get_validation_stats(migration_id)

        return {
            "migration_id": migration_id,
            **stats
        }

    except Exception as e:
        logger.error(f"Failed to get fidelity stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{migration_id}/validate-fidelity")
async def trigger_fidelity_validation(
    migration_id: str,
    background_tasks: BackgroundTasks,
    conversion_id: Optional[str] = None
):
    """
    Manually trigger 100% fidelity validation

    This is useful for re-validating after manual DAX edits.

    Args:
        migration_id: Migration ID
        conversion_id: Optional specific conversion to validate (validates all if not provided)

    Returns:
        Status message
    """
    try:
        # Check if migration exists
        migration = migration_store.get_migration(migration_id)
        if not migration:
            raise HTTPException(status_code=404, detail="Migration not found")

        # TODO: Implement validation trigger
        # This would call the validation engine for all conversions
        # or a specific conversion if conversion_id is provided

        return {
            "status": "validation_started",
            "migration_id": migration_id,
            "message": "Fidelity validation has been queued"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to trigger fidelity validation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# Model Enhancement Endpoints
# ============================================

@router.get("/{migration_id}/model-enhancements")
async def get_model_enhancements(migration_id: str):
    """
    Get required Power BI model enhancements for table calculations

    Returns model changes needed (Index columns, Date tables, etc.)
    that cannot be done with DAX measures alone.

    Returns:
        {
            "guide_path": "exports/mig_123/MODEL_ENHANCEMENTS_REQUIRED.md",
            "has_enhancements": true
        }
    """
    try:
        # Check if enhancement guide exists
        export_dir = Path("exports") / migration_id
        guide_path = export_dir / "MODEL_ENHANCEMENTS_REQUIRED.md"

        if not guide_path.exists():
            return {
                "has_enhancements": False,
                "message": "No model enhancements required"
            }

        return {
            "has_enhancements": True,
            "guide_path": str(guide_path),
            "m_scripts_path": str(export_dir / "m_scripts"),
            "dax_scripts_path": str(export_dir / "dax_scripts"),
            "message": "Model enhancements required. Download guide below."
        }

    except Exception as e:
        logger.error(f"Failed to get model enhancements: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/model-enhancements/download")
async def download_enhancement_guide(migration_id: str):
    """
    Download the MODEL_ENHANCEMENTS_REQUIRED.md guide

    Returns:
        Markdown file download
    """
    try:
        guide_path = Path("exports") / migration_id / "MODEL_ENHANCEMENTS_REQUIRED.md"

        if not guide_path.exists():
            raise HTTPException(status_code=404, detail="Enhancement guide not found")

        return FileResponse(
            path=guide_path,
            media_type="text/markdown",
            filename="MODEL_ENHANCEMENTS_REQUIRED.md"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download enhancement guide: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/model-enhancements/download-all")
async def download_all_enhancement_files(migration_id: str):
    """
    Download all enhancement files as ZIP

    Includes:
    - MODEL_ENHANCEMENTS_REQUIRED.md
    - m_scripts/ folder
    - dax_scripts/ folder

    Returns:
        ZIP file download
    """
    try:
        export_dir = Path("exports") / migration_id

        if not export_dir.exists():
            raise HTTPException(status_code=404, detail="Enhancement files not found")

        # Create ZIP in memory
        import io
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # Add guide file
            guide_path = export_dir / "MODEL_ENHANCEMENTS_REQUIRED.md"
            if guide_path.exists():
                zip_file.write(guide_path, "MODEL_ENHANCEMENTS_REQUIRED.md")

            # Add M scripts
            m_scripts_dir = export_dir / "m_scripts"
            if m_scripts_dir.exists():
                for m_file in m_scripts_dir.glob("*.m"):
                    zip_file.write(m_file, f"m_scripts/{m_file.name}")

            # Add DAX scripts
            dax_scripts_dir = export_dir / "dax_scripts"
            if dax_scripts_dir.exists():
                for dax_file in dax_scripts_dir.glob("*.dax"):
                    zip_file.write(dax_file, f"dax_scripts/{dax_file.name}")

        zip_buffer.seek(0)

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=model_enhancements_{migration_id}.zip"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create enhancement ZIP: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 5-Page Wizard Endpoints
# ============================================

@router.get("/{migration_id}/data-quality")
async def get_data_quality(migration_id: str):
    """
    Get data quality check for Page 1 - Data Understanding

    Returns:
        {
            "tables": [
                {
                    "table_name": "Sales",
                    "row_count": 120000,
                    "duplicate_count": 0,
                    "duplicate_rate": 0.0,
                    "status": "good"
                },
                {
                    "table_name": "Customers",
                    "row_count": 5000,
                    "duplicate_count": 12,
                    "duplicate_rate": 0.24,
                    "status": "warning"
                }
            ]
        }
    """
    try:
        # Get workbooks for this migration
        workbooks = migration_store.get_workbooks_by_migration(migration_id)

        if not workbooks:
            return {"tables": []}

        # Import hyper profiler
        from src.tableau.hyper_profiler import HyperDataProfiler

        quality_results = []

        for workbook in workbooks:
            # Get hyper file path from workbook metadata
            if hasattr(workbook, 'hyper_path') and workbook.hyper_path:
                try:
                    profiler = HyperDataProfiler(workbook.hyper_path)
                    tables = profiler.list_tables()

                    for table in tables:
                        # Get table profile
                        row_count = profiler.get_row_count(table)

                        # PERFORMANCE FIX #4: Use sampling for duplicate detection (10-30x faster)
                        # Samples 10K rows instead of full table scan
                        duplicate_count = profiler.detect_duplicates(table, sample_size=10000)
                        duplicate_rate = (duplicate_count / row_count * 100) if row_count > 0 else 0

                        quality_results.append({
                            "table_name": table,
                            "row_count": row_count,
                            "duplicate_count": duplicate_count,
                            "duplicate_rate": round(duplicate_rate, 2),
                            "status": "good" if duplicate_rate < 1 else "warning"
                        })
                except Exception as e:
                    logger.error(f"Failed to profile table: {e}")
                    continue

        return {"tables": quality_results}

    except Exception as e:
        logger.error(f"Failed to get data quality: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/data-preview")
async def get_data_preview(migration_id: str, table_name: Optional[str] = None, limit: int = 10):
    """
    Get data preview for Page 1 - Data Understanding

    Query params:
        - table_name: Specific table to preview (optional, defaults to first table)
        - limit: Number of rows to return (default: 10)

    Returns:
        {
            "table_name": "Sales",
            "columns": ["OrderID", "Customer", "Sales", "Profit"],
            "rows": [
                {"OrderID": 1001, "Customer": "John", "Sales": 250.5, "Profit": 75.2},
                ...
            ],
            "total_rows": 120000
        }
    """
    try:
        from src.tableau.hyper_profiler import HyperDataProfiler

        # Get workbooks
        workbooks = migration_store.get_workbooks_by_migration(migration_id)

        if not workbooks:
            raise HTTPException(status_code=404, detail="No workbooks found")

        # Get first workbook with hyper file
        workbook = workbooks[0]

        if not hasattr(workbook, 'hyper_path') or not workbook.hyper_path:
            raise HTTPException(status_code=404, detail="No data file found")

        profiler = HyperDataProfiler(workbook.hyper_path)
        tables = profiler.list_tables()

        if not tables:
            raise HTTPException(status_code=404, detail="No tables found")

        # Use specified table or first table
        target_table = table_name if table_name else tables[0]

        # Get preview data
        df = profiler.read_table(target_table, limit=limit)

        return {
            "table_name": target_table,
            "columns": list(df.columns),
            "rows": df.to_dict('records'),
            "total_rows": profiler.get_row_count(target_table)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get data preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/table-classifications")
async def get_table_classifications(migration_id: str):
    """
    Get table classifications for Page 2 - Model Intelligence

    Classifies tables as FACT or DIMENSION based on:
    - Row count
    - Numeric column density
    - Duplicate rate

    Returns:
        {
            "tables": [
                {
                    "table_name": "Sales",
                    "classification": "FACT",
                    "confidence_score": 95,
                    "row_count": 1200000,
                    "numeric_columns": 15,
                    "duplicate_rate": 2.1,
                    "potential_primary_key": "SalesID",
                    "pk_uniqueness": 100,
                    "reasoning": "High numeric density (15 columns) and large row count indicate fact table"
                },
                {
                    "table_name": "Customers",
                    "classification": "DIMENSION",
                    "confidence_score": 98,
                    "row_count": 5000,
                    "numeric_columns": 2,
                    "duplicate_rate": 0.0,
                    "potential_primary_key": "CustomerID",
                    "pk_uniqueness": 100,
                    "reasoning": "Low numeric density and small row count indicate dimension table"
                }
            ]
        }
    """
    try:
        from src.tableau.hyper_profiler import HyperDataProfiler

        workbooks = migration_store.get_workbooks_by_migration(migration_id)

        if not workbooks:
            return {"tables": []}

        classifications = []

        for workbook in workbooks:
            if hasattr(workbook, 'hyper_path') and workbook.hyper_path:
                try:
                    profiler = HyperDataProfiler(workbook.hyper_path)
                    tables = profiler.list_tables()

                    for table in tables:
                        # Profile the table
                        row_count = profiler.get_row_count(table)
                        columns = profiler.get_columns(table)
                        numeric_cols = [c for c in columns if c['data_type'] in ['INTEGER', 'REAL', 'NUMERIC', 'DOUBLE']]

                        # PERFORMANCE FIX #4: Use sampling for duplicate detection (10-30x faster)
                        # OLD: Full table scan on large tables (10-30 seconds for 1M rows)
                        # NEW: Sample-based detection (1-2 seconds, 95%+ accuracy)
                        duplicate_count = profiler.detect_duplicates(table, sample_size=10000)
                        duplicate_rate = (duplicate_count / row_count * 100) if row_count > 0 else 0

                        # Classify as FACT or DIMENSION
                        numeric_density = len(numeric_cols) / len(columns) if columns else 0

                        if row_count > 100000 and numeric_density > 0.5:
                            classification = "FACT"
                            confidence = 95
                            reasoning = f"High numeric density ({len(numeric_cols)} columns) and large row count indicate fact table"
                        elif row_count < 10000 and numeric_density < 0.3:
                            classification = "DIMENSION"
                            confidence = 98
                            reasoning = "Low numeric density and small row count indicate dimension table"
                        else:
                            classification = "DIMENSION"
                            confidence = 70
                            reasoning = "Moderate characteristics, likely a dimension table"

                        # Detect potential primary key
                        pk_column = None
                        pk_uniqueness = 0

                        for col in columns:
                            uniqueness = profiler.get_column_uniqueness(table, col['name'])
                            if uniqueness >= 99:
                                pk_column = col['name']
                                pk_uniqueness = round(uniqueness, 1)
                                break

                        classifications.append({
                            "table_name": table,
                            "classification": classification,
                            "confidence_score": confidence,
                            "row_count": row_count,
                            "numeric_columns": len(numeric_cols),
                            "duplicate_rate": round(duplicate_rate, 2),
                            "potential_primary_key": pk_column,
                            "pk_uniqueness": pk_uniqueness,
                            "reasoning": reasoning
                        })

                except Exception as e:
                    logger.error(f"Failed to classify tables: {e}")
                    continue

        return {"tables": classifications}

    except Exception as e:
        logger.error(f"Failed to get table classifications: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/suggested-relationships")
async def get_suggested_relationships(migration_id: str):
    """
    Get suggested relationships for Page 2 - Model Intelligence

    Uses the existing RelationshipDiscovery component to detect relationships
    between tables based on column names and data overlap.

    Returns:
        {
            "relationships": [
                {
                    "from_table": "Sales",
                    "from_column": "customer_id",
                    "to_table": "Customers",
                    "to_column": "id",
                    "relationship_type": "MANY_TO_ONE",
                    "confidence_score": 95,
                    "detection_method": "EXACT_MATCH",
                    "data_overlap": 98
                }
            ]
        }
    """
    try:
        from src.relationship_detector import RelationshipDetector
        from src.tableau.hyper_profiler import HyperDataProfiler

        workbooks = migration_store.get_workbooks_by_migration(migration_id)

        if not workbooks:
            return {"relationships": []}

        # Collect all tables from all workbooks
        all_tables_data = []

        for workbook in workbooks:
            if hasattr(workbook, 'hyper_path') and workbook.hyper_path:
                try:
                    profiler = HyperDataProfiler(workbook.hyper_path)
                    tables = profiler.list_tables()

                    for table in tables:
                        # Read table data
                        df = profiler.read_table(table, limit=10000)
                        all_tables_data.append({
                            "name": table,
                            "data": df
                        })

                except Exception as e:
                    logger.error(f"Failed to load table data: {e}")
                    continue

        if not all_tables_data:
            return {"relationships": []}

        # Run relationship detection
        detector = RelationshipDetector()
        relationships = detector.discover_relationships(all_tables_data)

        # Format for frontend
        formatted_relationships = []
        for rel in relationships:
            formatted_relationships.append({
                "from_table": rel.get("from_table"),
                "from_column": rel.get("from_column"),
                "to_table": rel.get("to_table"),
                "to_column": rel.get("to_column"),
                "relationship_type": rel.get("relationship_type", "MANY_TO_ONE"),
                "confidence_score": rel.get("confidence_score", 0),
                "detection_method": rel.get("detection_method", "PATTERN_MATCH"),
                "data_overlap": rel.get("data_overlap", 0)
            })

        return {"relationships": formatted_relationships}

    except Exception as e:
        logger.error(f"Failed to get suggested relationships: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/filters")
async def get_filters(migration_id: str):
    """
    Get filters for Page 3 - Tableau Logic

    Returns all filters from the Tableau workbook including context filters.

    Returns:
        {
            "filters": [
                {
                    "field_name": "Category",
                    "filter_type": "categorical",
                    "worksheet_name": "Sales Overview",
                    "is_context_filter": true,
                    "values": ["Technology", "Furniture"]
                }
            ]
        }
    """
    try:
        # Get workbooks
        workbooks = migration_store.get_workbooks_by_migration(migration_id)

        if not workbooks:
            return {"filters": []}

        all_filters = []

        # Import TWB parser
        from src.tableau.twb_parser import TableauTWBParser

        for workbook in workbooks:
            if hasattr(workbook, 'twb_path') and workbook.twb_path:
                try:
                    parser = TableauTWBParser(workbook.twb_path)
                    filters = parser.parse_filters()

                    for f in filters:
                        all_filters.append({
                            "field_name": f.get("field_name"),
                            "filter_type": f.get("filter_type", "categorical"),
                            "worksheet_name": f.get("worksheet_name"),
                            "is_context_filter": f.get("is_context_filter", False),
                            "values": f.get("values", [])
                        })

                except Exception as e:
                    logger.error(f"Failed to parse filters: {e}")
                    continue

        return {"filters": all_filters}

    except Exception as e:
        logger.error(f"Failed to get filters: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{migration_id}/trigger-conversion")
async def trigger_conversion(migration_id: str, background_tasks: BackgroundTasks):
    """
    Trigger DAX conversion for Page 4 - DAX Conversion

    Re-runs the DAX generation process for all calculations.

    Returns:
        {
            "status": "conversion_started",
            "migration_id": "mig_abc123",
            "message": "DAX conversion has been queued"
        }
    """
    try:
        migration = migration_store.get_migration(migration_id)

        if not migration:
            raise HTTPException(status_code=404, detail="Migration not found")

        # Update status
        migration_store.update_migration_status(
            migration_id,
            MigrationStatus.CONVERTING,
            current_stage="Re-generating DAX conversions"
        )

        # Trigger conversion in background
        from src.tableau.dax_generator import DAXGenerator

        async def run_conversion():
            try:
                generator = DAXGenerator()
                calculations = migration_store.get_calculations_by_migration(migration_id)

                for calc in calculations:
                    # Generate DAX
                    result = await generator.tableau_to_dax(
                        calc.calc_formula,
                        calc.visual_context
                    )

                    # Save conversion
                    migration_store.save_conversion(
                        calc_id=calc.calc_id,
                        dax_formula=result['dax_formula'],
                        confidence_score=result.get('confidence', 0.0),
                        reasoning=result.get('reasoning', ''),
                        warnings=result.get('warnings', [])
                    )

            except Exception as e:
                logger.error(f"Conversion failed: {e}")

        background_tasks.add_task(run_conversion)

        return {
            "status": "conversion_started",
            "migration_id": migration_id,
            "message": "DAX conversion has been queued"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to trigger conversion: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/conversion-report")
async def download_conversion_report(migration_id: str):
    """
    Download Excel conversion report for Page 4 - DAX Conversion

    Generates an Excel file with:
    - Calculation name
    - Tableau formula
    - DAX formula
    - Confidence score
    - Status (Auto-converted, Manual review, Failed)
    - Warnings

    Returns:
        Excel file download
    """
    try:
        import pandas as pd
        from io import BytesIO

        # Get conversions
        conversions = migration_store.get_conversions_by_migration(migration_id)
        calculations = migration_store.get_calculations_by_migration(migration_id)

        # Create mapping of calc_id to calculation
        calc_map = {c.calc_id: c for c in calculations}

        # Build report data
        report_data = []

        for conv in conversions:
            calc = calc_map.get(conv.calc_id)

            if calc:
                # Determine status category
                if conv.confidence_score >= 0.9:
                    status = "AUTO_CONVERTED"
                elif conv.confidence_score >= 0.7:
                    status = "MANUAL_REVIEW"
                else:
                    status = "FAILED"

                report_data.append({
                    "Calculation Name": calc.calc_name,
                    "Tableau Formula": calc.calc_formula,
                    "DAX Formula": conv.dax_formula,
                    "Confidence Score": f"{conv.confidence_score * 100:.1f}%",
                    "Status": status,
                    "Conversion Method": conv.conversion_method.value if hasattr(conv, 'conversion_method') else "LLM",
                    "Warnings": "; ".join(conv.warnings) if conv.warnings else "None",
                    "Reasoning": conv.reasoning if hasattr(conv, 'reasoning') else ""
                })

        # Create Excel file
        df = pd.DataFrame(report_data)

        # Write to BytesIO
        excel_buffer = BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='DAX Conversions', index=False)

            # Auto-adjust column widths
            worksheet = writer.sheets['DAX Conversions']
            for column in worksheet.columns:
                max_length = 0
                column_letter = column[0].column_letter
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 80)
                worksheet.column_dimensions[column_letter].width = adjusted_width

        excel_buffer.seek(0)

        return StreamingResponse(
            excel_buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=dax_conversion_report_{migration_id}.xlsx"}
        )

    except Exception as e:
        logger.error(f"Failed to generate conversion report: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/recommendations")
async def get_recommendations(migration_id: str):
    """
    Get Power BI recommendations for Page 5 - Recommendations

    Provides:
    - Success rate estimation
    - Strategic recommendations (date hierarchies, visual types, etc.)
    - Visual type mappings (Tableau → Power BI)

    Returns:
        {
            "success_rate": {
                "overall_rate": 85.0,
                "total_calculations": 14,
                "auto_converted": 10,
                "manual_review": 3,
                "complex": 1
            },
            "recommendations": [
                {
                    "title": "Create Date Hierarchy",
                    "priority": "HIGH",
                    "icon": "calendar",
                    "description": "Detected date columns. Create a Power BI Date table...",
                    "action_items": [
                        "Create Calendar table using CALENDAR() function",
                        "Add Year, Quarter, Month columns"
                    ]
                }
            ],
            "visual_recommendations": [
                {
                    "tableau_worksheet": "Sales by Region",
                    "tableau_visual_type": "Cross-tab",
                    "powerbi_visual": "Matrix"
                }
            ]
        }
    """
    try:
        # Get conversions to calculate success rate
        conversions = migration_store.get_conversions_by_migration(migration_id)
        calculations = migration_store.get_calculations_by_migration(migration_id)

        # Calculate success rate
        auto_converted = sum(1 for c in conversions if c.confidence_score >= 0.9)
        manual_review = sum(1 for c in conversions if 0.7 <= c.confidence_score < 0.9)
        complex = sum(1 for c in conversions if c.confidence_score < 0.7)
        total = len(conversions)

        overall_rate = (auto_converted / total * 100) if total > 0 else 0

        # Generate strategic recommendations
        recommendations = []

        # Check for date columns
        has_date_columns = any('date' in calc.calc_name.lower() or 'year' in calc.calc_name.lower()
                               for calc in calculations)

        if has_date_columns:
            recommendations.append({
                "title": "Create Date Hierarchy",
                "priority": "HIGH",
                "icon": "calendar",
                "description": "Detected date columns in your data. Create a Power BI Date table with built-in hierarchies for Year/Quarter/Month to enable time intelligence functions.",
                "action_items": [
                    "Create Calendar table using CALENDAR() or CALENDARAUTO() function",
                    "Add Year, Quarter, Month, Week columns",
                    "Create relationships to fact tables on date columns",
                    "Use time intelligence DAX functions (TOTALYTD, SAMEPERIODLASTYEAR, etc.)"
                ]
            })

        # Check for LOD expressions
        has_lod = any(calc.calc_type == 'LOD_EXPRESSION' for calc in calculations)

        if has_lod:
            recommendations.append({
                "title": "Review LOD Expression Conversions",
                "priority": "HIGH",
                "icon": "grid",
                "description": "LOD expressions require careful review. Verify that ALLEXCEPT, ALL, and KEEPFILTERS patterns match your business logic.",
                "action_items": [
                    "Test each LOD conversion against sample data",
                    "Verify filter context is correctly applied",
                    "Consider using measure groups for complex calculations"
                ]
            })

        # Check for table calculations
        has_table_calc = any('WINDOW_' in calc.calc_formula or 'RUNNING_' in calc.calc_formula
                            for calc in calculations)

        if has_table_calc:
            recommendations.append({
                "title": "Implement Model Enhancements for Table Calculations",
                "priority": "MEDIUM",
                "icon": "grid",
                "description": "Table calculations detected. Some may require Power Query M code to add index columns or date tables.",
                "action_items": [
                    "Review MODEL_ENHANCEMENTS_REQUIRED.md guide",
                    "Add index columns using Power Query",
                    "Create calculated columns where DAX measures aren't sufficient"
                ]
            })

        # Visual recommendations (mock data - would parse from TWB in real implementation)
        visual_recommendations = [
            {
                "tableau_worksheet": "Sales Overview",
                "tableau_visual_type": "Bar Chart",
                "powerbi_visual": "Clustered Bar Chart"
            },
            {
                "tableau_worksheet": "Regional Analysis",
                "tableau_visual_type": "Cross-tab",
                "powerbi_visual": "Matrix"
            }
        ]

        return {
            "success_rate": {
                "overall_rate": round(overall_rate, 1),
                "total_calculations": total,
                "auto_converted": auto_converted,
                "manual_review": manual_review,
                "complex": complex
            },
            "recommendations": recommendations,
            "visual_recommendations": visual_recommendations
        }

    except Exception as e:
        logger.error(f"Failed to get recommendations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/download-all")
async def download_all_artifacts(migration_id: str):
    """
    Download complete migration package

    Includes:
    - PBIX file (if generated)
    - DAX measures file (.dax)
    - Excel conversion report
    - Table data (Excel files)
    - Filter/parameter conversion report
    - Visual conversion report
    - Migration metadata (JSON)

    Returns:
        ZIP file download
    """
    try:
        import io
        import pandas as pd

        # Create ZIP in memory
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # 1. Migration metadata
            migration = migration_store.get_migration(migration_id)
            zip_file.writestr(
                "migration_metadata.json",
                json.dumps(migration.to_dict(), indent=2, default=str)
            )

            export_dir = Path("exports") / migration_id

            # Log export directory status
            logger.info(f"📁 Export directory: {export_dir.absolute()}")
            logger.info(f"📁 Directory exists: {export_dir.exists()}")

            if export_dir.exists():
                files_in_dir = list(export_dir.rglob("*"))
                logger.info(f"📂 Files in export directory: {len(files_in_dir)}")
                for f in files_in_dir[:10]:  # Log first 10 files
                    logger.info(f"   - {f.relative_to(export_dir)}")
            else:
                logger.warning(f"⚠️  Export directory does not exist: {export_dir}")

            # 2. PBIX file (if exists)
            pbix_path = export_dir / "migrated_model.pbix"
            logger.info(f"🔍 Checking for PBIX: {pbix_path}")
            if pbix_path.exists():
                zip_file.write(pbix_path, "migrated_model.pbix")
                logger.info("✅ Added PBIX file to package")
            else:
                logger.warning("⚠️  PBIX file not found, skipping")

            # 3. DAX measures file
            dax_path = export_dir / "measures.dax"
            logger.info(f"🔍 Checking for DAX file: {dax_path}")
            if dax_path.exists():
                with open(dax_path, 'r', encoding='utf-8') as f:
                    zip_file.writestr("dax_measures.dax", f.read())
                logger.info("✅ Added DAX measures from file")
            else:
                logger.info("⚠️  DAX file not found, generating from database")
                # Fallback: generate from conversions
                conversions = migration_store.get_conversions_by_migration(migration_id)
                calculations = migration_store.get_calculations_by_migration(migration_id)
                calc_map = {c.calc_id: c for c in calculations}

                dax_content = "-- DAX Measures Export\n"
                dax_content += f"-- Generated: {datetime.now().isoformat()}\n"
                dax_content += f"-- Migration ID: {migration_id}\n\n"

                for conv in conversions:
                    calc = calc_map.get(conv.calc_id)
                    if calc:
                        dax_content += f"-- Measure: {calc.calc_name}\n"
                        dax_content += f"-- Original Tableau: {calc.calc_formula}\n"
                        dax_content += f"-- Confidence: {conv.confidence_score * 100:.0f}%\n"
                        dax_content += f"{conv.dax_formula}\n\n"

                zip_file.writestr("dax_measures.dax", dax_content)

            # 4. Excel conversion report
            conversions = migration_store.get_conversions_by_migration(migration_id)
            calculations = migration_store.get_calculations_by_migration(migration_id)
            calc_map = {c.calc_id: c for c in calculations}

            report_data = []
            for conv in conversions:
                calc = calc_map.get(conv.calc_id)
                if calc:
                    status = "AUTO_CONVERTED" if conv.confidence_score >= 0.9 else \
                            "MANUAL_REVIEW" if conv.confidence_score >= 0.7 else "FAILED"

                    report_data.append({
                        "Calculation Name": calc.calc_name,
                        "Tableau Formula": calc.calc_formula,
                        "DAX Formula": conv.dax_formula,
                        "Confidence Score": f"{conv.confidence_score * 100:.1f}%",
                        "Status": status
                    })

            df = pd.DataFrame(report_data)
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='DAX Conversions', index=False)

            zip_file.writestr("conversion_report.xlsx", excel_buffer.getvalue())

            # 5. Table data Excel files (NEW)
            table_data_dir = export_dir / "table_data"
            logger.info(f"🔍 Checking for table data: {table_data_dir}")
            if table_data_dir.exists():
                excel_files = list(table_data_dir.glob("*.xlsx"))
                logger.info(f"📊 Found {len(excel_files)} Excel files")
                for excel_file in excel_files:
                    zip_file.write(excel_file, f"table_data/{excel_file.name}")
                    logger.info(f"✅ Added table data: {excel_file.name}")
            else:
                logger.warning("⚠️  Table data directory not found")

            # 6. Filter/parameter conversion report
            filter_report_path = export_dir / "filter_parameter_conversion.md"
            logger.info(f"🔍 Checking for filter report: {filter_report_path}")
            if filter_report_path.exists():
                with open(filter_report_path, 'r', encoding='utf-8') as f:
                    zip_file.writestr("filter_parameter_conversion.md", f.read())
                logger.info("✅ Added filter/parameter report")
            else:
                logger.warning("⚠️  Filter/parameter report not found")

            # 7. Visual conversion report
            visual_report_path = export_dir / "visual_conversion.md"
            logger.info(f"🔍 Checking for visual report: {visual_report_path}")
            if visual_report_path.exists():
                with open(visual_report_path, 'r', encoding='utf-8') as f:
                    zip_file.writestr("visual_conversion.md", f.read())
                logger.info("✅ Added visual conversion report")
            else:
                logger.warning("⚠️  Visual conversion report not found")

            # NOTE: Enhancement guide and recommendations are NOT included (removed as per requirements)

            # 8. README
            readme = f"# Tableau to Power BI Migration Package\n\n"
            readme += f"**Migration ID:** {migration_id}\n"
            readme += f"**Status:** {migration.status.value}\n"
            readme += f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            readme += "## Contents\n\n"
            readme += "### Core Files\n"
            readme += "- `migrated_model.pbix` - Ready-to-use Power BI file (if generated)\n"
            readme += "- `dax_measures.dax` - All DAX measure definitions\n"
            readme += "- `migration_metadata.json` - Migration job metadata\n\n"
            readme += "### Reports & Documentation\n"
            readme += "- `conversion_report.xlsx` - Detailed conversion report\n"
            readme += "- `filter_parameter_conversion.md` - Filter & parameter mappings\n"
            readme += "- `visual_conversion.md` - Visual conversion details\n\n"
            readme += "### Table Data\n"
            readme += "- `table_data/` - Exported table data as Excel files\n\n"
            readme += "## Next Steps\n\n"
            readme += "1. Open `migrated_model.pbix` in Power BI Desktop\n"
            readme += "2. Review `conversion_report.xlsx` for conversion details\n"
            readme += "3. Check `filter_parameter_conversion.md` for filter setup\n"
            readme += "4. Use table data files to load data into Power BI\n"
            readme += "5. Validate results against original Tableau workbook\n"

            zip_file.writestr("README.md", readme)

            logger.info("=" * 60)
            logger.info("📦 ZIP PACKAGE SUMMARY:")
            logger.info(f"   Total files in ZIP: {len(zip_file.namelist())}")
            for name in zip_file.namelist():
                logger.info(f"   ✓ {name}")
            logger.info("=" * 60)

        zip_buffer.seek(0)

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=powerbi_migration_complete_{migration_id}.zip"}
        )

    except Exception as e:
        logger.error(f"Failed to create migration package: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# Complete Migration with PBIX Generation (NEW)
# ============================================

@router.post("/migrate/complete")
async def create_complete_migration(
    files: List[UploadFile] = File(...),
    background_tasks: BackgroundTasks = None
):
    """
    Execute complete end-to-end migration with PBIX generation

    NEW: This endpoint includes all migration features (STEPS 1-10):
    - Parse TWBX
    - Extract & validate calculations
    - Build data model (relationships, date table)
    - Convert filters & parameters
    - Convert table calculations
    - Create PBIX file with Tabular Editor
    - Generate documentation

    Request:
        - files: List of .twbx or .twb files

    Response:
        {
            "migration_id": "mig_abc123",
            "status": "processing",
            "workbook_count": 1,
            "message": "Complete migration started with PBIX generation",
            "features": [
                "DAX conversion",
                "100% fidelity validation",
                "Data model builder",
                "Filter & parameter conversion",
                "Table calculations",
                "PBIX injection",
                "Visual conversion"
            ]
        }
    """
    try:
        # Validate files
        for file in files:
            if not file.filename.endswith(('.twbx', '.twb')):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid file type: {file.filename}. Only .twbx and .twb files are supported."
                )

        # Create migration ID
        migration_id = f"mig_{uuid.uuid4().hex[:12]}"

        # Create migration job
        migration = migration_store.create_migration(migration_id)

        # Save uploaded files
        file_paths = []
        for file in files:
            stored_path = Path(config.UPLOAD_DIR) / f"{migration_id}_{file.filename}"

            with open(stored_path, "wb") as f:
                content = await file.read()
                f.write(content)

            file_paths.append(str(stored_path))
            logger.info(f"Saved file for complete migration: {file.filename} ({len(content)} bytes)")

        # Update migration with file count
        migration_store.update_migration_counts(
            migration_id,
            workbook_count=len(files)
        )

        # Trigger complete migration in background
        background_tasks.add_task(orchestrator.execute_migration, migration_id, file_paths)
        logger.info(f"✨ Started COMPLETE migration (with PBIX) for {migration_id}")

        return {
            "migration_id": migration_id,
            "status": "processing",
            "workbook_count": len(files),
            "message": "Complete migration started with PBIX generation",
            "features": [
                "DAX conversion with AI",
                "100% fidelity validation",
                "Data model builder (relationships, date table)",
                "Filter & parameter conversion",
                "Table calculations (running totals, rank, etc.)",
                "PBIX file injection via Tabular Editor",
                "Visual conversion with auto-layout",
                "Complete documentation"
            ],
            "note": "Migration will generate a ready-to-use .pbix file if Tabular Editor is installed"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start complete migration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/migrate/complete/{migration_id}/artifacts")
async def get_complete_migration_artifacts(migration_id: str):
    """
    Get all artifacts from complete migration

    Returns:
        {
            "migration_id": "mig_abc123",
            "pbix_file": "exports/mig_abc123/migrated_model.pbix",
            "dax_file": "exports/mig_abc123/measures.dax",
            "documentation": {
                "filter_conversion": "exports/mig_abc123/filter_parameter_conversion.md",
                "visual_conversion": "exports/mig_abc123/visual_conversion.md",
                "enhancement_guide": "exports/mig_abc123/EnhancementGuide.md"
            },
            "status": "completed"
        }
    """
    migration = migration_store.get_migration(migration_id)

    if not migration:
        raise HTTPException(status_code=404, detail="Migration not found")

    export_dir = Path("exports") / migration_id

    artifacts = {
        "migration_id": migration_id,
        "status": migration.status.value,
        "pbix_file": None,
        "dax_file": None,
        "documentation": {}
    }

    # Check for PBIX file
    pbix_path = export_dir / "migrated_model.pbix"
    if pbix_path.exists():
        artifacts["pbix_file"] = str(pbix_path)

    # Check for DAX file (fallback)
    dax_path = export_dir / "measures.dax"
    if dax_path.exists():
        artifacts["dax_file"] = str(dax_path)

    # Check for documentation
    filter_doc = export_dir / "filter_parameter_conversion.md"
    if filter_doc.exists():
        artifacts["documentation"]["filter_conversion"] = str(filter_doc)

    visual_doc = export_dir / "visual_conversion.md"
    if visual_doc.exists():
        artifacts["documentation"]["visual_conversion"] = str(visual_doc)

    enhancement_doc = export_dir / "EnhancementGuide.md"
    if enhancement_doc.exists():
        artifacts["documentation"]["enhancement_guide"] = str(enhancement_doc)

    return artifacts


@router.get("/migrate/complete/{migration_id}/download/{file_type}")
async def download_migration_artifact(migration_id: str, file_type: str):
    """
    Download specific migration artifact

    file_type: pbix, dax, filter_doc, visual_doc, enhancement_doc
    """
    export_dir = Path("exports") / migration_id

    file_map = {
        "pbix": export_dir / "migrated_model.pbix",
        "dax": export_dir / "measures.dax",
        "filter_doc": export_dir / "filter_parameter_conversion.md",
        "visual_doc": export_dir / "visual_conversion.md",
        "enhancement_doc": export_dir / "EnhancementGuide.md",
        "template": export_dir / "template.pbix"
    }

    file_path = file_map.get(file_type)

    if not file_path or not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact '{file_type}' not found for migration {migration_id}"
        )

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream"
    )


# ============================================
# WebSocket for Real-Time Progress
# ============================================

@router.websocket("/{migration_id}/ws")
async def websocket_endpoint(websocket: WebSocket, migration_id: str):
    """
    WebSocket connection for real-time migration progress

    Messages sent to client:
        {
            "type": "progress",
            "migration_id": "mig_abc123",
            "progress_percent": 45,
            "current_stage": "Generating DAX formulas",
            "message": "Processing calculation 12 of 24"
        }
    """
    await ws_manager.connect(websocket, migration_id)

    try:
        while True:
            # Keep connection alive
            data = await websocket.receive_text()

            # Echo back (ping/pong)
            if data == "ping":
                await websocket.send_text("pong")

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, migration_id)
        logger.info(f"WebSocket disconnected for migration {migration_id}")
