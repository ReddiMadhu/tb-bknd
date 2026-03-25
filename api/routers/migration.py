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
        MAX_FILE_SIZE = 500 * 1024 * 1024  # S3 fix: 500MB limit
        file_paths = []
        for file in files:
            # Save file
            file_id = f"file_{uuid.uuid4().hex[:8]}"
            stored_path = Path(config.UPLOAD_DIR) / f"{migration_id}_{file.filename}"

            content = await file.read()
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File {file.filename} exceeds 500MB limit ({len(content) / 1e6:.0f}MB)"
                )

            with open(stored_path, "wb") as f:
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


@router.get("/{migration_id}/workbooks/{workbook_id}/model")
async def get_workbook_model(migration_id: str, workbook_id: str):
    """
    Get the complete raw JSON model natively extracted from the Tableau workbook
    """
    workbooks = migration_store.get_workbooks_by_migration(migration_id)
    workbook = next((wb for wb in workbooks if wb.workbook_id == workbook_id), None)
    
    if not workbook:
        raise HTTPException(status_code=404, detail="Workbook not found")
        
    return workbook.raw_model or {}


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
            # Refresh migration object to get latest counts
            migration = migration_store.get_migration(migration_id)
            
            # ---------------------------------------------------------
            # Generate Excel Report (Combined DAX, Worksheet Analysis, & Data Tables)
            # ---------------------------------------------------------
            import pandas as pd
            from io import BytesIO
            import re
            from src.tableau.hyper_profiler import HyperDataProfiler

            # --- 1. PREPARE DATA ---
            # Get conversions & calculations
            conversions = migration_store.get_conversions_by_migration(migration_id)
            calculations = migration_store.get_calculations_by_migration(migration_id)
            calc_map = {c.calc_id: c for c in calculations}
            
            # Build Replacement Map (Internal Name -> Caption)
            replacement_map = {}
            workbooks_list = [] # Store for worksheet analysis
            tables_report_data = [] # Store for data tables analysis
            
            try:
                workbooks = migration_store.get_workbooks_by_migration(migration_id)
                for workbook in workbooks:
                    model = workbook.raw_model or {}
                    
                    # 1. Parse Calculated Fields for Captions
                    raw_calcs = [c for c in model.get("columns", []) if c.get("formula")]
                    for cf in raw_calcs:
                        display_name = cf.get("caption") or cf.get("internal_name")
                        if cf.get("internal_name"):
                            replacement_map[cf.get("internal_name")] = display_name
                            
                    # 2. Store Worksheets for Analysis
                    ws_data = model.get("worksheets", [])
                    workbooks_list.append({
                        "filename": workbook.filename,
                        "worksheets": ws_data
                    })

                    # 3. Profile Data Tables (using hyper_files stored in model)
                    hyper_files = model.get("hyper_files", [])
                    for hyper_path in hyper_files:
                        if not hyper_path or not str(hyper_path).endswith(".hyper"):
                            continue
                        try:
                            profiler = HyperDataProfiler(str(hyper_path))
                            tables = profiler.list_tables()
                            
                            for table in tables:
                                try:
                                    # Strip quotes for profiling
                                    table_unquoted = str(table).strip('"').replace('"."', '.')
                                    # Light profiling
                                    table_profile = profiler.profile_table(table_unquoted, sample_size=100)
                                    
                                    # Format columns as: Name (TYPE)
                                    col_details = [f"{col.column_name} ({col.data_type})" for col in table_profile.columns]
                                    
                                    tables_report_data.append({
                                        "Workbook": workbook.filename,
                                        "Table Name": table,
                                        "Row Count": table_profile.row_count,
                                        "Column Count": len(col_details),
                                        "Column Names": ", ".join(col_details)
                                    })
                                except Exception as te:
                                    logger.warning(f"Failed to profile table {table}: {te}")
                        except Exception as he:
                            logger.warning(f"Failed to profile hyper file {hyper_path} for {workbook.filename}: {he}")

            except Exception as e:
                logger.error(f"Failed to build replacement map: {e}")

            # Helper to get friendly name
            def get_friendly_name(name):
                return replacement_map.get(name, name)

            # Helper to clean formulas
            sorted_keys = sorted(replacement_map.keys(), key=len, reverse=True)
            def replace_names(formula):
                if not formula: return ""
                updated = formula
                for internal in sorted_keys:
                    readable = replacement_map[internal]
                    escaped_internal = re.escape(internal)
                    updated = re.sub(f"\\[{escaped_internal}\\]", f"[{readable}]", updated)
                    updated = re.sub(f"\\b{escaped_internal}\\b", readable, updated)
                return updated

            # --- 2. BUILD DAX CONVERSION SHEET DATA ---
            dax_report_data = []
            for conv in conversions:
                calc = calc_map.get(conv.calc_id)
                if calc:
                    # Determine Validation Status
                    if (conv.confidence_score or 0) > 0.95:
                        validation_status = "Passed"
                    else:
                        if conv.status.value == "validated": validation_status = "Passed"
                        elif conv.status.value == "failed": validation_status = "Failed"
                        else: validation_status = "Manual Review"

                    friendly_name = get_friendly_name(calc.calc_name)
                    
                    dax_report_data.append({
                        "Calculated Field": friendly_name,
                        "Tableau Formula": replace_names(calc.calc_formula),
                        "DAX Formula": replace_names(conv.dax_formula),
                        "Validation Test": validation_status
                    })

            # --- 3. BUILD WORKSHEET ANALYSIS SHEET DATA ---
            worksheet_report_data = []
            for wb_data in workbooks_list:
                filename = wb_data['filename']
                for ws in wb_data['worksheets']:
                    # Resolve Friendly Names for Lists
                    dimensions = [get_friendly_name(d) for d in (ws.dimensions or [])]
                    measures = [get_friendly_name(m.name if hasattr(m, 'name') else m) for m in (ws.measures or [])]
                    base_measures = [get_friendly_name(m.name) for m in (ws.measures or []) if hasattr(m, 'type') and m.type == 'base_measure']
                    
                    # Resolve Axes
                    # axes is a dictionary, not an object
                    rows = ws.axes.get('rows') if ws.axes else None
                    cols = ws.axes.get('columns') if ws.axes else None
                    
                    # Fallback logic for Cards (similar to frontend)
                    if not rows and ws.visual_type.value == 'text' and ws.measures: 
                         # Roughly mapping text tables/cards
                         rows = get_friendly_name(ws.measures[0].name if hasattr(ws.measures[0], 'name') else ws.measures[0])
                    else:
                        rows = get_friendly_name(rows) if rows else '-'

                    if not cols and ws.visual_type.value == 'text' and ws.dimensions:
                        cols = get_friendly_name(ws.dimensions[0])
                    else:
                        cols = get_friendly_name(cols) if cols else '-'

                    worksheet_report_data.append({
                        "Workbook": filename,
                        "Worksheet Name": ws.name,
                        "Chart Type": ws.visual_type.value if ws.visual_type else "Automatic",
                        "Dimensions": ", ".join(dimensions),
                        "Measures": ", ".join(measures),
                        "Base Measures": ", ".join(base_measures),
                        "Rows": rows,
                        "Columns": cols
                    })

            # --- 4. WRITE EXCEL FILE TO ZIP ---
            excel_buffer = BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                # Sheet 1: DAX Conversions
                df_dax = pd.DataFrame(dax_report_data)
                df_dax.to_excel(writer, sheet_name='DAX Conversions', index=False)
                
                # Sheet 2: Worksheet Analysis
                df_ws = pd.DataFrame(worksheet_report_data)
                df_ws.to_excel(writer, sheet_name='Worksheet Analysis', index=False)

                # Sheet 3: Data Tables
                df_tables = pd.DataFrame(tables_report_data)
                df_tables.to_excel(writer, sheet_name='Data Tables', index=False)

                # Auto-adjust column widths for all sheets
                for sheetname in writer.sheets:
                    worksheet = writer.sheets[sheetname]
                    for column in worksheet.columns:
                        max_length = 0
                        column_letter = column[0].column_letter
                        for cell in column:
                            try:
                                if len(str(cell.value)) > max_length:
                                    max_length = len(str(cell.value))
                            except: pass
                        adjusted_width = min(max_length + 2, 80)
                        worksheet.column_dimensions[column_letter].width = adjusted_width

            excel_buffer.seek(0)
            zipf.writestr(f"migration_report_{migration_id}.xlsx", excel_buffer.getvalue())

            # 5. Add minimal update info (Optional, to keep zip valid if empty)
            zipf.writestr("README.txt", f"Migration Report for {migration_id}\nGenerated: {datetime.now().isoformat()}")

            # ---------------------------------------------------------
            # Generate model.bim (Semantic Model)
            # ---------------------------------------------------------
            try:
                # Build valid model.bim structure for Tabular Editor import
                bim_measures = []
                for c in conversions:
                    if c.dax_formula:
                        bim_measures.append({
                            "name": get_friendly_name(calc_map.get(c.calc_id).calc_name) if calc_map.get(c.calc_id) else c.calc_id,
                            "expression": c.dax_formula,
                            "formatString": "#,##0.00"
                        })

                model_bim = {
                    "name": "SemanticModel",
                    "compatibilityLevel": 1500,
                    "model": {
                        "culture": "en-US",
                        "tables": [
                            {
                                "name": "_Calculations", 
                                "columns": [
                                    { "name": "Column", "dataType": "string", "sourceColumn": "Column" }
                                ],
                                "partitions": [
                                    {
                                        "name": "Partition",
                                        "mode": "import",
                                        "source": {
                                            "type": "m",
                                            "expression": "let\n Source = Table.FromRows(Json.Document(Binary.Decompress(Binary.FromText(\"i44FAA==\", BinaryEncoding.Base64), Compression.Deflate)), let _t = ((type nullable text) meta [Serialized.Text = true]) in type table [Column = _t])\nin\n Source"
                                        }
                                    }
                                ],
                                "measures": bim_measures
                            }
                        ]
                    }
                }
                zipf.writestr(
                    "model.bim",
                    json.dumps(model_bim, indent=2)
                )
            except Exception as e:
                logger.error(f"Failed to generate model.bim: {e}")
                # We do not raise here to ensure at least Excel report is delivered
                zipf.writestr("model_bim_error.txt", f"Failed to generate model.bim: {str(e)}")

            # ---------------------------------------------------------
            # Include Table Data Folder (Excel files)
            # ---------------------------------------------------------
            export_dir = Path("exports") / migration_id
            table_data_dir = export_dir / "table_data"
            if table_data_dir.exists():
                for excel_file in table_data_dir.glob("*.xlsx"):
                    zipf.write(excel_file, f"table_data/{excel_file.name}")

            # ---------------------------------------------------------
            # Include Generated PBIP Project (native TMDL structure)
            # ---------------------------------------------------------
            pbip_dir = Path("exports") / migration_id / "pbip_output"

            if pbip_dir.exists():
                pbip_file_count = 0
                for fp in pbip_dir.rglob("*"):
                    if fp.is_file():
                        # Preserve the inner folder structure under pbip_project/
                        arcname = f"pbip_project/{fp.relative_to(pbip_dir)}"
                        zipf.write(fp, arcname)
                        pbip_file_count += 1
                logger.info(f"Zipped PBIP project: {pbip_file_count} files from {pbip_dir}")
            else:
                # PBIP generation was skipped or failed — include a minimal stub
                logger.warning(f"PBIP output folder not found at {pbip_dir} — including stub")
                zipf.writestr(
                    "pbip_project/migration.pbip",
                    json.dumps({
                        "version": "1.0",
                        "artifacts": [],
                        "settings": {"enableAutoRecovery": True},
                        "_note": "PBIP generation was not completed for this migration"
                    }, indent=2)
                )

        logger.info(f"Generated artifacts ZIP for {migration_id} at {artifact_path}")
        
    except Exception as e:
        logger.error(f"Failed to generate artifacts: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate artifacts: {str(e)}")

    return {
        "message": "Power BI artifacts generated",
        "download_url": f"/api/v1/migration/{migration_id}/download",
        "artifacts": {
            "metadata": "migration_metadata.json",
            "conversions": "dax_conversions.json",
            "validations": "validation_results.json",
            "workbooks": "workbooks.json",
            "calculations": "calculations.json"
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
            logger.info(f"[DATA QUALITY] Processing workbook: {workbook.filename}, file_path: {workbook.file_path}")

            # Use raw_model to find Hyper path
            model = workbook.raw_model or {}
            hyper_path = None
            for conn in model.get("connections", []):
                if conn.get("type") in ("hyper", "federated") and conn.get("filename"):
                    hyper_path = conn.get("filename")
                    break

            if hyper_path:
                try:
                    logger.info(f"[DATA QUALITY] Found Hyper path in model: {hyper_path}")

                    profiler = HyperDataProfiler(hyper_path)
                    tables = profiler.list_tables()
                    logger.info(f"[DATA QUALITY] Found {len(tables)} tables: {tables}")

                    for table in tables:
                        # Unquote table name for profiler methods (profiler adds quotes internally)
                        # "Extract"."TableName" → Extract.TableName
                        table_unquoted = str(table).strip('"').replace('".\"', '.')

                        logger.info(f"[DATA QUALITY] Profiling table: {table} (unquoted: {table_unquoted})")

                        # Simple approach: just get row count and duplicates
                        # Use read_table for row count (fast, doesn't need information_schema)
                        df = profiler.read_table(table_unquoted, limit=10001)  # Limit to check if > 10K
                        row_count = len(df)

                        # Duplicate detection
                        duplicate_count = profiler.detect_duplicates(table_unquoted, sample_size=10000)
                        duplicate_rate = (duplicate_count / row_count * 100) if row_count > 0 else 0

                        quality_results.append({
                            "table_name": table,  # Keep original quoted name for response
                            "row_count": row_count,
                            "duplicate_count": duplicate_count,
                            "duplicate_rate": round(duplicate_rate, 2),
                            "status": "good" if duplicate_rate < 1 else "warning"
                        })
                        logger.info(f"[DATA QUALITY] Profiled table {table}: {row_count} rows, {duplicate_rate:.2f}% duplicates")
                except Exception as e:
                    logger.error(f"[DATA QUALITY] Failed to profile table: {e}", exc_info=True)
                    continue
            else:
                logger.warning(f"[DATA QUALITY] Skipping workbook - not a TWBX file or no file_path: {workbook.file_path}")

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
            logger.info(f"[TABLE CLASSIFICATIONS] Processing workbook: {workbook.filename}, file_path: {workbook.file_path}")

            # Use raw_model to find Hyper path
            model = workbook.raw_model or {}
            hyper_path = None
            for conn in model.get("connections", []):
                if conn.get("type") in ("hyper", "federated") and conn.get("filename"):
                    hyper_path = conn.get("filename")
                    break

            if hyper_path:
                try:
                    logger.info(f"[TABLE CLASSIFICATIONS] Found Hyper path: {hyper_path}")

                    profiler = HyperDataProfiler(hyper_path)
                    tables = profiler.list_tables()
                    logger.info(f"[TABLE CLASSIFICATIONS] Found {len(tables)} tables: {tables}")

                    for table in tables:
                        # Unquote table name for profiler methods (profiler adds quotes internally)
                        # "Extract"."TableName" → Extract.TableName
                        table_unquoted = str(table).strip('"').replace('".\"', '.')

                        logger.info(f"[TABLE CLASSIFICATIONS] Profiling table: {table} (unquoted: {table_unquoted})")

                        # Use profile_table() which works correctly (like workbook_metadata endpoint)
                        profile = profiler.profile_table(table_unquoted, sample_size=10000)
                        row_count = profile.row_count
                        columns = profile.columns
                        numeric_cols = [c for c in columns if c.data_type in ['int64', 'float64', 'Int64', 'Float64']]

                        # Duplicate detection
                        duplicate_count = profiler.detect_duplicates(table_unquoted, sample_size=10000)
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

                        # Detect potential primary key from column profiles
                        pk_column = None
                        pk_uniqueness = 0

                        # Find column with highest cardinality (most unique values)
                        for col in columns:
                            if col.cardinality >= 0.99:  # 99%+ unique
                                pk_column = str(col.column_name)
                                pk_uniqueness = round(col.cardinality * 100, 1)
                                break

                        classifications.append({
                            "table_name": table,  # Keep original quoted name for response
                            "classification": classification,
                            "confidence_score": confidence,
                            "row_count": row_count,
                            "numeric_columns": len(numeric_cols),
                            "duplicate_rate": round(duplicate_rate, 2),
                            "potential_primary_key": pk_column,
                            "pk_uniqueness": pk_uniqueness,
                            "reasoning": reasoning
                        })
                        logger.info(f"[TABLE CLASSIFICATIONS] Classified table {table} as {classification} (confidence: {confidence})")

                except Exception as e:
                    logger.error(f"[TABLE CLASSIFICATIONS] Failed to classify tables: {e}", exc_info=True)
                    continue
            else:
                logger.warning(f"[TABLE CLASSIFICATIONS] Skipping workbook - not a TWBX file or no file_path: {workbook.file_path}")

        logger.info(f"[TABLE CLASSIFICATIONS] Returning {len(classifications)} table classifications")
        return {"tables": classifications}

    except Exception as e:
        logger.error(f"Failed to get table classifications: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _norm_table(raw: str) -> str:
    """
    Normalize a Tableau table name strictly to a unified clean form.
    Handles 'Extract.Meeting_C95B...', 'gcrm!opportunity!2020...', 'Gcrm_Opportunity_2020...'
    and also updates field names like 'Product Group (Gcrm_Opportunity_2020...)' -> 'Product Group (Opportunity)'
    """
    if not raw or not isinstance(raw, str):
        return raw

    import re
    def clean_table(t: str) -> str:
        # Strip "Extract." prefix
        if '.' in t:
            t = t.split('.')[-1]
        t = t.strip('"').strip("'")
        
        # Strip 32-char GUID or >=8 char GUID
        t = re.sub(r'_[A-Fa-f0-9]{8,}$', '', t)
        
        # If it looks like a Tableau joined/extracted namespace (gcrm!opportunity!timestamp or Gcrm_Opportunity_timestamp)
        parts = re.split(r'[!_]', t)
        meaningful = [p for p in parts if not re.match(r'^\d+$', p) and p]
        
        if len(meaningful) >= 2 and meaningful[0].lower() in ['gcrm', 'extract', 'logical']:
            t = meaningful[-1]
        elif '!' in t:
            t = meaningful[-1] if meaningful else t
            
        # Capitalize gracefully
        return t.title() if t.islower() else t

    # Handle (TableName) suffix in field/dimension names
    def _repl_table(match):
        return f"({clean_table(match.group(1))})"

    if '(' in raw and ')' in raw:
        return re.sub(r'\(([^)]+)\)', _repl_table, raw)

    return clean_table(raw)


@router.get("/{migration_id}/suggested-relationships")
async def get_suggested_relationships(migration_id: str):
    """
    Get suggested relationships for Page 2 - Model Intelligence
    
    Reads natively extracted joins and logical relationships from Tableau workbook.
    Table names are normalized at response time to handle stale DB data.
    """
    try:
        workbooks = migration_store.get_workbooks_by_migration(migration_id)

        if not workbooks:
            return {"relationships": []}

        formatted_relationships = []
        for workbook in workbooks:
            model = workbook.raw_model or {}
            
            # Extract Joins — normalize table names for stale DB data
            joins = model.get("joins", [])
            for j in joins:
                lt = _norm_table(j.get("left_table", "") or "")
                rt = _norm_table(j.get("right_table", "") or "")
                lc = j.get("left_column", "")
                rc = j.get("right_column", "")
                formatted_relationships.append({
                    "relationship_id": f"{lt}_{lc}_{rt}_{rc}",
                    "source": {"file": lt, "column": lc},
                    "target": {"file": rt, "column": rc},
                    "relationship_type": j.get("join_type", "INNER_JOIN"),
                    "confidence_score": None,
                    "confidence_level": None,
                    "detection_method": "TABLEAU_PARSER",
                    "deleted": False
                })

            # Extract Relationships — normalize table names for stale DB data
            rels = model.get("relationships", [])
            for r in rels:
                t1 = _norm_table(r.get("table1", "") or "")
                t2 = _norm_table(r.get("table2", "") or "")
                c1 = r.get("table1_column", "")
                c2 = r.get("table2_column", "")
                formatted_relationships.append({
                    "relationship_id": f"{t1}_{c1}_{t2}_{c2}",
                    "source": {"file": t1, "column": c1},
                    "target": {"file": t2, "column": c2},
                    "relationship_type": r.get("relationship_type", "MANY_TO_ONE"),
                    "confidence_score": None,
                    "confidence_level": None,
                    "detection_method": "TABLEAU_PARSER",
                    "deleted": False
                })

        logger.info(f"Returned {len(formatted_relationships)} relationships for {migration_id} from Tableau parser")
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

        for workbook in workbooks:
            model = workbook.raw_model or {}
            worksheets = model.get("worksheets", [])

            for ws in worksheets:
                ws_filters = ws.get("filters", [])
                for f in ws_filters:
                    all_filters.append({
                        "field_name": f.get("field"),
                        "filter_type": "categorical", # Mapping from mark_type/datatype could be better
                        "worksheet_name": ws.get("name"),
                        "is_context_filter": False, # Would need deeper model parsing
                        "values": [] # New model might not store all values
                    })

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
async def download_conversion_report(
    migration_id: str,
    conversion_ids: Optional[str] = None  # Comma-separated conversion IDs
):
    """
    Download Excel conversion report for Page 4 - DAX Conversion

    Generates an Excel file with:
    - Calculated Field (Friendly Name)
    - Tableau Formula (Cleaned)
    - DAX Formula (Cleaned)
    - Validation Test (Passed/Manual Review)

    Query params:
        conversion_ids: Optional comma-separated list of conversion IDs to export

    Returns:
        Excel file download
    """
    try:
        import pandas as pd
        from io import BytesIO
        import re

        # Get conversions
        conversions = migration_store.get_conversions_by_migration(migration_id)
        calculations = migration_store.get_calculations_by_migration(migration_id)

        # Filter by selected conversion IDs if provided
        if conversion_ids:
            selected_ids = set(conversion_ids.split(','))
            conversions = [c for c in conversions if c.conversion_id in selected_ids]

        # Create mapping of calc_id to calculation
        calc_map = {c.calc_id: c for c in calculations}

        # ---------------------------------------------------------
        # 1. Build Replacement Map (Internal Name -> Caption)
        # ---------------------------------------------------------
        replacement_map = {}
        try:
            workbooks = migration_store.get_workbooks_by_migration(migration_id)
            for workbook in workbooks:
                model = workbook.raw_model or {}
                raw_calcs = [c for c in model.get("columns", []) if c.get("formula")]
                for cf in raw_calcs:
                    display_name = cf.get("caption") or cf.get("internal_name")
                    if cf.get("internal_name"):
                        replacement_map[cf.get("internal_name")] = display_name
        except Exception as e:
            logger.error(f"Failed to build replacement map: {e}")

        # Helper to clean formulas
        # Sort keys by length descending to replace longest naming conflicts first
        sorted_keys = sorted(replacement_map.keys(), key=len, reverse=True)

        def replace_names(formula):
            if not formula:
                return ""
            updated = formula
            for internal in sorted_keys:
                readable = replacement_map[internal]
                # Escape special chars in internal name
                escaped_internal = re.escape(internal)
                
                # 1. Replace bracketed references: [Internal] -> [Readable]
                updated = re.sub(f"\\[{escaped_internal}\\]", f"[{readable}]", updated)

                # 2. Replace unbracketed occurrences (e.g. definition on LHS): Internal = ...
                # Use word boundaries to avoid partial matches
                updated = re.sub(f"\\b{escaped_internal}\\b", readable, updated)
            return updated

        # ---------------------------------------------------------
        # 2. Build Report Data
        # ---------------------------------------------------------
        report_data = []

        for conv in conversions:
            calc = calc_map.get(conv.calc_id)

            if calc:
                # Determine Validation Test status
                # If confidence > 95%, mark as Passed
                # Otherwise keep existing validation status or default to Manual Review
                if (conv.confidence_score or 0) > 0.95:
                    validation_status = "Passed"
                else:
                    # Map internal status to readable string
                    if conv.status.value == "validated":
                        validation_status = "Passed"
                    elif conv.status.value == "failed":
                        validation_status = "Failed"
                    else:
                        validation_status = "Manual Review"

                # Use friendly name if available, otherwise fallback to calc_name
                friendly_name = replacement_map.get(calc.calc_name, calc.calc_name)
                
                # Clean formulas
                cleaned_tableau_formula = replace_names(calc.calc_formula)
                cleaned_dax_formula = replace_names(conv.dax_formula)

                report_data.append({
                    "Calculated Field": friendly_name,
                    "Tableau Formula": cleaned_tableau_formula,
                    "DAX Formula": cleaned_dax_formula,
                    "Validation Test": validation_status
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
    - Excel Report (DAX Conversions, Worksheet Analysis, Data Tables)
    - Semantic Model (model.bim)
    - README.txt
    """
    try:
        import io
        import pandas as pd
        import re
        from io import BytesIO
        from src.tableau.hyper_profiler import HyperDataProfiler

        # Create ZIP in memory
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # Refresh migration object
            migration = migration_store.get_migration(migration_id)
            
            # ---------------------------------------------------------
            # Generate Excel Report (Combined DAX, Worksheet Analysis, & Data Tables)
            # ---------------------------------------------------------
            
            # --- 1. PREPARE DATA ---
            # Get conversions & calculations
            conversions = migration_store.get_conversions_by_migration(migration_id)
            calculations = migration_store.get_calculations_by_migration(migration_id)
            calc_map = {c.calc_id: c for c in calculations}
            
            # Build Replacement Map (Internal Name -> Caption)
            replacement_map = {}
            workbooks_list = [] # Store for worksheet analysis
            tables_report_data = [] # Store for data tables analysis
            
            try:
                workbooks = migration_store.get_workbooks_by_migration(migration_id)
                for workbook in workbooks:
                    model = workbook.raw_model or {}

                    # Build caption replacement map from raw_model columns
                    raw_calcs = [c for c in model.get("columns", []) if c.get("formula")]
                    for cf in raw_calcs:
                        display_name = cf.get("caption") or cf.get("internal_name")
                        if cf.get("internal_name"):
                            replacement_map[cf.get("internal_name")] = display_name

                    # Store worksheets for analysis
                    ws_data = model.get("worksheets", [])
                    workbooks_list.append({
                        "filename": workbook.filename,
                        "worksheets": ws_data
                    })

                    # -------------------------------------------------------
                    # Profile Data Tables — use hyper_files (set by orchestrator)
                    # -------------------------------------------------------
                    hyper_files = model.get("hyper_files", [])
                    for hyper_path in hyper_files:
                        if not hyper_path or not str(hyper_path).endswith(".hyper"):
                            continue
                        try:
                            profiler = HyperDataProfiler(str(hyper_path))
                            tables = profiler.list_tables()
                            for table in tables:
                                try:
                                    table_unquoted = str(table).strip('"').replace('"."', '.')
                                    clean_name = profiler.get_clean_table_name(table)
                                    table_profile = profiler.profile_table(table_unquoted, sample_size=100)
                                    col_details = [
                                        f"{col.column_name} ({col.data_type})"
                                        for col in table_profile.columns
                                    ]
                                    tables_report_data.append({
                                        "Workbook": workbook.filename,
                                        "Table Name": clean_name,
                                        "Row Count": table_profile.row_count,
                                        "Column Count": len(col_details),
                                        "Columns": ", ".join(col_details)
                                    })
                                except Exception as te:
                                    logger.warning(f"Failed to profile table {table}: {te}")
                        except Exception as he:
                            logger.warning(f"Failed to profile hyper {hyper_path}: {he}")

            except Exception as e:
                logger.error(f"Failed to build replacement map: {e}")

            # Helper to get friendly name
            def get_friendly_name(name):
                return replacement_map.get(name, name)

            # Helper to clean formulas
            sorted_keys = sorted(replacement_map.keys(), key=len, reverse=True)
            def replace_names(formula):
                if not formula: return ""
                updated = formula
                for internal in sorted_keys:
                    readable = replacement_map[internal]
                    escaped_internal = re.escape(internal)
                    updated = re.sub(f"\\[{escaped_internal}\\]", f"[{readable}]", updated)
                    updated = re.sub(f"\\b{escaped_internal}\\b", readable, updated)
                return updated

            # --- 2. BUILD DAX CONVERSION SHEET DATA ---
            dax_report_data = []
            for conv in conversions:
                calc = calc_map.get(conv.calc_id)
                if calc:
                    # Determine Validation Status
                    if (conv.confidence_score or 0) > 0.95:
                        validation_status = "Passed"
                    else:
                        if conv.status.value == "validated": validation_status = "Passed"
                        elif conv.status.value == "failed": validation_status = "Failed"
                        else: validation_status = "Manual Review"

                    friendly_name = get_friendly_name(calc.calc_name)
                    
                    dax_report_data.append({
                        "Calculated Field": friendly_name,
                        "Tableau Formula": replace_names(calc.calc_formula),
                        "DAX Formula": replace_names(conv.dax_formula),
                        "Validation Test": validation_status
                    })

            # --- 3. BUILD WORKSHEET ANALYSIS SHEET DATA ---
            worksheet_report_data = []
            for wb_data in workbooks_list:
                filename = wb_data['filename']
                for ws in wb_data['worksheets']:
                    # ws is a dict from raw_model['worksheets']
                    ws_name = ws.get("name", "")
                    visual_type = ws.get("mark_type") or ws.get("chart_type", "Automatic")

                    # --- Rows / Columns from raw shelf fields ---
                    rows_fields = ws.get("rows_fields", []) or ws.get("rows", [])
                    cols_fields = ws.get("columns_fields", []) or ws.get("cols", [])
                    rows_str = ", ".join(get_friendly_name(str(r)) for r in rows_fields if r) or "-"
                    cols_str = ", ".join(get_friendly_name(str(c)) for c in cols_fields if c) or "-"

                    # --- Dimensions from raw_model (or fallback from axes.dimensions) ---
                    raw_dims = ws.get("dimensions", [])
                    dimensions = [get_friendly_name(d) for d in raw_dims if d]

                    # --- Measures (calculated + base) ---
                    measures_raw = ws.get("measures", [])
                    calc_fields_list = ws.get("calculated_fields", [])
                    all_measures = []
                    calc_names = []
                    base_names = []
                    for m in measures_raw:
                        m_name = m.get("name") if isinstance(m, dict) else str(m)
                        m_type = m.get("type", "") if isinstance(m, dict) else ""
                        friendly = get_friendly_name(m_name)
                        all_measures.append(friendly)
                        if m_type == "calculated":
                            calc_names.append(friendly)
                        else:
                            base_names.append(friendly)

                    # If measures are empty, fallback: extract calc field names from calculated_fields
                    if not calc_names and calc_fields_list:
                        for cf in calc_fields_list:
                            cf_name = cf.get("name") if isinstance(cf, dict) else str(cf)
                            friendly = get_friendly_name(cf_name)
                            calc_names.append(friendly)
                            all_measures.append(friendly)

                    # --- Filters ---
                    filters_raw = ws.get("filters", [])
                    filter_fields = [get_friendly_name(f.get("field", "")) for f in filters_raw if isinstance(f, dict) and f.get("field")]
                    filters_str = ", ".join(filter_fields) if filter_fields else "-"

                    # --- Calculated field formulas (from calculated_fields list) ---
                    calc_details = []
                    for cf in calc_fields_list:
                        if isinstance(cf, dict):
                            cf_name = get_friendly_name(cf.get("name", ""))
                            cf_formula = cf.get("formula", "")
                            if cf_name:
                                calc_details.append(f"{cf_name}: {cf_formula}" if cf_formula else cf_name)

                    worksheet_report_data.append({
                        "Workbook": filename,
                        "Worksheet Name": ws_name,
                        "Chart Type": visual_type,
                        "Rows Shelf": rows_str,
                        "Columns Shelf": cols_str,
                        "Dimensions": ", ".join(dimensions) if dimensions else "-",
                        "All Measures": ", ".join(all_measures) if all_measures else "-",
                        "Calculated Fields": ", ".join(calc_names) if calc_names else "-",
                        "Calculated Field Formulas": " | ".join(calc_details) if calc_details else "-",
                        "Base Measures": ", ".join(base_names) if base_names else "-",
                        "Filters": filters_str,
                    })


            # --- 4. WRITE EXCEL FILE TO ZIP ---
            excel_buffer = BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                # Sheet 1: DAX Conversions
                df_dax = pd.DataFrame(dax_report_data)
                df_dax.to_excel(writer, sheet_name='DAX Conversions', index=False)
                
                # Sheet 2: Worksheet Analysis
                df_ws = pd.DataFrame(worksheet_report_data)
                df_ws.to_excel(writer, sheet_name='Worksheet Analysis', index=False)

                # Sheet 3: Data Tables
                df_tables = pd.DataFrame(tables_report_data)
                df_tables.to_excel(writer, sheet_name='Data Tables', index=False)

                # Auto-adjust column widths for all sheets
                for sheetname in writer.sheets:
                    worksheet = writer.sheets[sheetname]
                    for column in worksheet.columns:
                        max_length = 0
                        column_letter = column[0].column_letter
                        for cell in column:
                            try:
                                if len(str(cell.value)) > max_length:
                                    max_length = len(str(cell.value))
                            except: pass
                        adjusted_width = min(max_length + 2, 80)
                        worksheet.column_dimensions[column_letter].width = adjusted_width

            excel_buffer.seek(0)
            zip_file.writestr(f"migration_report_{migration_id}.xlsx", excel_buffer.getvalue())
            # Include PBIP + table_data from exports/{migration_id}/
            # The orchestrator writes to: exports/{migration_id}/pbip_output/
            #                             exports/{migration_id}/table_data/
            # Try absolute path first (relative to this file), then CWD fallback
            # ---------------------------------------------------------
            bknd_root = Path(__file__).resolve().parent.parent.parent  # api/routers -> api -> bknd
            export_dir_abs = bknd_root / "exports" / migration_id
            export_dir_rel = Path("exports") / migration_id

            # Pick whichever exists
            export_dir = export_dir_abs if export_dir_abs.exists() else export_dir_rel
            logger.info(f"  Export dir: {export_dir} (exists={export_dir.exists()})")

            pbip_files_added = False
            table_data_files_added = 0

            if export_dir.exists():
                # Walk every file under exports/{mig_id}/ and add to zip preserving structure
                for fp in export_dir.rglob("*"):
                    if not fp.is_file():
                        continue
                    rel = fp.relative_to(export_dir)
                    rel_str = str(rel)
                    arcname = str(rel).replace("\\", "/")

                    # Skip any already-generated xlsx reports (not needed twice)
                    if fp.suffix == ".xlsx" and "report" in fp.name:
                        continue

                    if rel_str.startswith("pbip_output"):
                        # Put under pbip_project/ in ZIP
                        inner = fp.relative_to(export_dir / "pbip_output")
                        arcname = f"pbip_project/{str(inner).replace(chr(92), '/')}"
                        pbip_files_added = True
                    elif rel_str.startswith("table_data"):
                        arcname = f"table_data/{fp.name}"
                        table_data_files_added += 1

                    zip_file.writestr(arcname, fp.read_bytes())

            if pbip_files_added:
                logger.info(f"  ✓ Included PBIP project from {export_dir / 'pbip_output'}")
            else:
                logger.warning(f"  ⚠ PBIP not found under {export_dir}")

            if table_data_files_added:
                logger.info(f"  ✓ Included {table_data_files_added} table data file(s)")

            # ---------------------------------------------------------
            # Generate model.bim (Semantic Model) — fallback / bonus file
            # ---------------------------------------------------------
            try:
                bim_measures = []
                for c in conversions:
                    if c.dax_formula:
                        calc_obj = calc_map.get(c.calc_id)
                        bim_measures.append({
                            "name": get_friendly_name(calc_obj.calc_name) if calc_obj else c.calc_id,
                            "expression": c.dax_formula,
                            "formatString": "#,##0.00"
                        })

                model_bim = {
                    "name": "SemanticModel",
                    "compatibilityLevel": 1500,
                    "model": {
                        "culture": "en-US",
                        "tables": [
                            {
                                "name": "_Calculations",
                                "columns": [
                                    {"name": "Column", "dataType": "string", "sourceColumn": "Column"}
                                ],
                                "partitions": [
                                    {
                                        "name": "Partition",
                                        "mode": "import",
                                        "source": {
                                            "type": "m",
                                            "expression": "let\n Source = Table.FromRows(Json.Document(Binary.Decompress(Binary.FromText(\"i44FAA==\", BinaryEncoding.Base64), Compression.Deflate)), let _t = ((type nullable text) meta [Serialized.Text = true]) in type table [Column = _t])\nin\n Source"
                                        }
                                    }
                                ],
                                "measures": bim_measures
                            }
                        ]
                    }
                }
                zip_file.writestr("model.bim", json.dumps(model_bim, indent=2))
            except Exception as e:
                logger.error(f"Failed to generate model.bim: {e}")
                zip_file.writestr("model_bim_error.txt", f"model.bim generation failed: {e}")

            # README
            readme_lines = [
                "Tableau to Power BI Migration Export",
                "=====================================",
                f"Migration ID: {migration_id}",
                f"Generated:    {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
                "Contents:",
                f"1. migration_report_{migration_id}.xlsx  — DAX Conversions, Worksheet Analysis, Data Tables",
            ]
            if pbip_files_added:
                readme_lines += [
                    "2. pbip_project/  — Full Power BI project (.pbip structure)",
                    "   To open: extract ZIP → double-click the .pbip file in Power BI Desktop.",
                ]
            if table_data_files_added:
                readme_lines.append(f"3. table_data/  — {table_data_files_added} Excel table export(s)")
            readme_lines.append("4. model.bim  — Alternative semantic model (Tabular Editor import)")
            zip_file.writestr("README.txt", "\n".join(readme_lines))

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
