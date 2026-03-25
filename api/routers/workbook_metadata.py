"""Workbook Metadata API Router - Comprehensive Tableau workbook inspection"""
from fastapi import APIRouter, HTTPException, Body
from typing import List, Dict, Any, Optional
from pathlib import Path
from loguru import logger
from pydantic import BaseModel
import pandas as pd
import uuid

from storage.migration_store import MigrationStore
from storage.preview_store import PreviewStore
from src.tableau.hyper_profiler import HyperDataProfiler
from src.tableau.tableau_extractor import extract_tableau_model # For fallback profiling if needed
import re


router = APIRouter(prefix="/api/v1/migration", tags=["workbook-metadata"])

# Initialize stores
migration_store = MigrationStore()
preview_store = PreviewStore()


def _resolve_hyper_path(model: dict, migration_id: Optional[str] = None) -> Optional[str]:
    """
    Robustly resolve the path to a .hyper file for a given workbook model.
    Handles old cache, missing files, and temp paths.
    """
    import os
    from pathlib import Path
    
    # Optional typing import done globally or locally
    # Pyre complains about api.config missing from path analysis,
    # but the API app runs from root where 'api' is a module.
    from api.config import config
    
    potential_paths = []
    
    # 1. Check hyper_files list stored by migration orchestrator
    for hf in model.get("hyper_files", []):
        if hf and str(hf).endswith(".hyper"):
            potential_paths.append(str(hf))
            
    # 2. Check connections info
    for conn in model.get("connections", []):
        if conn.get("type") in ("hyper", "federated") and conn.get("filename"):
            potential_paths.append(str(conn.get("filename")))
            
    # 3. Check tables metadata
    for t in model.get("tables", []):
        if t.get("source") and ".hyper" in t.get("source"):
            potential_paths.append(str(t.get("source")))
            
    # Verify paths exist
    for path in potential_paths:
        if os.path.exists(path):
            return path
            
    # 4. Fallback for static/old cache demos: Search in data/uploads
    try:
        uploads_dir = Path(config.UPLOAD_DIR)
        if uploads_dir.exists():
            for f in uploads_dir.glob("*.hyper"):
                if f.is_file():
                    logger.warning(f"Using fallback hyper file found in uploads: {f}")
                    return str(f)
    except Exception as e:
        logger.error(f"Error checking fallback hyper files: {e}")

    return None


def _norm_table(raw: str) -> str:
    """
    Normalize a Tableau table name strictly to a unified clean form.
    Handles 'Extract.Meeting_C95B...', 'gcrm!opportunity!2020...', 'Gcrm_Opportunity_2020...'
    and also updates field names like 'Product Group (Gcrm_Opportunity_2020...)' -> 'Product Group (Opportunity)'
    """
    if not raw or not isinstance(raw, str):
        return raw

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


# Request model for tableau preview
class TableauPreviewRequest(BaseModel):
    table_names: List[str]


# ============================================
# PERFORMANCE FIX #6: Split Large Endpoint
# ============================================
# Split workbook metadata into smaller, focused endpoints
# OLD: Single 5-10MB response taking 8+ seconds
# NEW: Multiple small endpoints, load data on-demand

@router.get("/{migration_id}/workbook-metadata/summary")
async def get_workbook_metadata_summary(migration_id: str):
    """
    FAST: Get lightweight workbook metadata summary (counts only)

    Returns basic counts without heavy data profiling.
    Use this for initial page load, then fetch details on-demand.

    Response time: <500ms (was 8+ seconds for full endpoint)
    """
    try:
        migration = migration_store.get_migration(migration_id)
        if not migration:
            raise HTTPException(status_code=404, detail="Migration not found")

        workbooks = migration_store.get_workbooks_by_migration(migration_id)
        if not workbooks:
            raise HTTPException(status_code=404, detail="No workbooks found")

        # Build lightweight summary (no data profiling)
        workbooks_summary = []

        for workbook in workbooks:
            try:
                # Use raw_model from database (pre-parsed)
                model = workbook.raw_model or {}

                # Get counts from model
                worksheets = model.get("worksheets", [])
                dashboards = model.get("dashboards", [])
                # Filter columns for those that are calculations
                calculated_fields = [c for c in model.get("columns", []) if c.get("formula")]
                parameters = model.get("parameters", [])
                data_sources = model.get("connections", [])

                # Get table names (if profiled during discovery)
                # Normalize via _norm_table to handle stale DB data with raw ! names or GUID suffixes
                tables_raw        = model.get("tables", [])
                table_names       = [_norm_table(t.get("name", "")) for t in tables_raw]
                clean_table_names = [_norm_table(t.get("display_name") or t.get("name", "")) for t in tables_raw]

                workbooks_summary.append({
                    "workbook_id": workbook.workbook_id,
                    "filename": workbook.filename,
                    "worksheet_count": len(worksheets),
                    "dashboard_count": len(dashboards),
                    "calculated_field_count": len(calculated_fields),
                    "parameter_count": len(parameters),
                    "data_source_count": len(data_sources),
                    "table_count": len(table_names),
                    "table_names": clean_table_names,
                    "table_names_raw": table_names
                })

            except Exception as e:
                logger.error(f"Error parsing workbook {workbook.filename}: {e}")
                workbooks_summary.append({
                    "workbook_id": workbook.workbook_id,
                    "filename": workbook.filename,
                    "error": str(e)
                })

        return {
            "migration_id": migration_id,
            "workbook_count": len(workbooks),
            "workbooks": workbooks_summary
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get workbook metadata summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/workbook-metadata/{workbook_id}/worksheets")
async def get_workbook_worksheets(migration_id: str, workbook_id: str):
    """
    Get worksheet details for a specific workbook (lazy loaded)
    """
    try:
        workbooks = migration_store.get_workbooks_by_migration(migration_id)
        workbook = next((wb for wb in workbooks if wb.workbook_id == workbook_id), None)

        if not workbook:
            raise HTTPException(status_code=404, detail="Workbook not found")

        model = workbook.raw_model or {}
        worksheets_raw = model.get("worksheets", [])

        # Map unique calculations for resolution
        calc_map = {c.get("internal_name"): c for c in model.get("columns", []) if c.get("formula")}

        worksheets = []
        for ws in worksheets_raw:
            # Resolve measures
            resolved_measures = []
            # In new model, ws['measures'] might be more structured or a list of field names
            # Adjust based on tableau_extractor output
            ws_measures = ws.get("measures", [])
            for m in ws_measures:
                m_name = m.get("name") if isinstance(m, dict) else m
                if m_name in calc_map:
                    cf = calc_map[m_name]
                    resolved_measures.append({
                        "type": "calculated",
                        "name": cf.get("caption") or cf.get("internal_name"),
                        "formula": cf.get("formula")
                    })
                else:
                    resolved_measures.append({
                        "type": "base_measure",
                        "name": m_name
                    })

            def _format_axis_simple(shelf_list):
                formatted = []
                for item in shelf_list:
                    import re as _re
                    m = _re.search(r'\[([^\]]+)\]', str(item))
                    name = m.group(1) if m else str(item).strip("[] ")
                    
                    # Try to map to caption if it exists in calc_map
                    col = calc_map.get(name)
                    label = _norm_table(col.get("caption") or name) if col else _norm_table(name)
                    formatted.append(label)
                return ", ".join(formatted)

            axes_dict = {
                "rows": _format_axis_simple(ws.get("rows", [])),
                "columns": _format_axis_simple(ws.get("cols", []))
            }

            worksheets.append({
                "worksheet_name": ws.get("name", ""),
                "chart_type": ws.get("mark_type", "Automatic"),
                "axes": axes_dict,
                "dimensions": ws.get("dimensions", []),
                "measures": resolved_measures,
                "name": ws.get("name", ""),
                "visual_type": ws.get("mark_type", ""),
                "rows_fields": ws.get("rows", []),
                "columns_fields": ws.get("cols", []),
                "filters": [f.get("field") for f in ws.get("filters", [])]
            })

        return {
            "workbook_id": workbook_id,
            "worksheets": worksheets
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get worksheets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/workbook-metadata/{workbook_id}/calculated-fields")
async def get_workbook_calculated_fields(migration_id: str, workbook_id: str):
    """
    Get calculated fields for a specific workbook (lazy loaded)
    """
    try:
        workbooks = migration_store.get_workbooks_by_migration(migration_id)
        workbook = next((wb for wb in workbooks if wb.workbook_id == workbook_id), None)

        if not workbook:
            raise HTTPException(status_code=404, detail="Workbook not found")

        model = workbook.raw_model or {}
        calculated_fields_raw = [c for c in model.get("columns", []) if c.get("formula")]

        calculated_fields = [
            {
                "name": cf.get("internal_name", ""),
                "formula": cf.get("formula", ""),
                "calc_type": cf.get("formula_type", "standard"),
                "datatype": cf.get("datatype", ""),
                "role": cf.get("role", ""),
                "caption": cf.get("caption")
            }
            for cf in calculated_fields_raw
        ]

        return {
            "workbook_id": workbook_id,
            "calculated_fields": calculated_fields
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get calculated fields: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/workbook-metadata/{workbook_id}/table/{table_name}")
async def get_table_details(migration_id: str, workbook_id: str, table_name: str):
    """
    Get detailed table information (lazy loaded on-demand)

    This is the heavy operation - only call when user actually views the table
    """
    try:
        workbooks = migration_store.get_workbooks_by_migration(migration_id)
        workbook = next((wb for wb in workbooks if wb.workbook_id == workbook_id), None)

        if not workbook:
            raise HTTPException(status_code=404, detail="Workbook not found")

        # Find hyper files from model or fallback locations
        model = workbook.raw_model or {}
        hyper_path = _resolve_hyper_path(model, migration_id)

        if not hyper_path:
            raise HTTPException(status_code=404, detail="No Hyper files paths found in metadata")

        profiler = HyperDataProfiler(hyper_path)

        # Profile just this one table
        table_unquoted = table_name.strip('"').replace('"."', '.')
        table_profile = profiler.profile_table(table_unquoted, sample_size=100)

        # Get data preview
        df = profiler.read_table(table_unquoted, limit=10)

        # Convert to JSON-safe format
        data_preview_raw = df.to_dict('records')
        data_preview = []
        for row in data_preview_raw:
            clean_row = {}
            for key, value in row.items():
                if pd.isna(value):
                    clean_row[str(key)] = None
                elif isinstance(value, (pd.Timestamp, pd.DatetimeTZDtype)):
                    clean_row[str(key)] = str(value)
                elif isinstance(value, (int, float, str, bool)):
                    clean_row[str(key)] = value
                else:
                    clean_row[str(key)] = str(value)
            data_preview.append(clean_row)

        # Extract column info
        columns_info = []
        for col_profile in table_profile.columns:
            data_type = str(col_profile.data_type).upper()
            if 'INT' in data_type:
                data_type = 'INTEGER'
            elif 'FLOAT' in data_type or 'DOUBLE' in data_type:
                data_type = 'REAL'
            elif 'OBJECT' in data_type or 'STR' in data_type:
                data_type = 'TEXT'
            elif 'BOOL' in data_type:
                data_type = 'BOOLEAN'

            columns_info.append({
                "name": col_profile.column_name,
                "data_type": data_type,
                "nullable": col_profile.null_count > 0,
                "distinct_count": col_profile.distinct_count,
                "null_count": col_profile.null_count
            })

        return {
            "table_name": table_name,
            "row_count": table_profile.row_count,
            "columns": columns_info,
            "data_preview": data_preview,
            "primary_key_candidates": table_profile.primary_key_candidates
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get table details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# Keep original endpoint for backward compatibility
# ============================================

@router.get("/{migration_id}/workbook-metadata")
async def get_comprehensive_workbook_metadata(migration_id: str):
    """
    Get comprehensive Tableau workbook metadata for Page 1 - Data Understanding

    Returns ALL extracted information:
    - Worksheet names
    - Dashboard names
    - Field names (all columns from data sources)
    - Calculated field names + formulas
    - Table names
    - Column names per table
    - Data preview for each table
    - Data sources
    - Parameters
    - Filters

    Example Response:
        {
            "migration_id": "mig_abc123",
            "workbooks": [
                {
                    "workbook_id": "wb_001",
                    "filename": "sales_dashboard.twbx",
                    "worksheets": [
                        {
                            "name": "Sales Overview",
                            "visual_type": "bar",
                            "rows_fields": ["Region", "Category"],
                            "columns_fields": ["Year"],
                            "marks_fields": ["Sales"],
                            "filters": ["Year"]
                        }
                    ],
                    "dashboards": [
                        {
                            "name": "Executive Dashboard",
                            "worksheets_included": ["Sales Overview", "Trends"]
                        }
                    ],
                    "calculated_fields": [
                        {
                            "name": "Profit Ratio",
                            "formula": "SUM([Profit]) / SUM([Sales])",
                            "calc_type": "tableau",
                            "datatype": "real",
                            "role": "measure"
                        }
                    ],
                    "parameters": [
                        {
                            "name": "Date Granularity",
                            "datatype": "string",
                            "current_value": "Month",
                            "allowable_values": ["Year", "Quarter", "Month", "Day"]
                        }
                    ],
                    "data_sources": [
                        {
                            "name": "Sales Data",
                            "connection_type": "hyper",
                            "tables": ["Extract", "Orders"],
                            "fields": ["OrderID", "Customer", "Sales", "Profit"],
                            "table_details": [
                                {
                                    "table_name": "Extract",
                                    "row_count": 9994,
                                    "columns": [
                                        {
                                            "name": "Row ID",
                                            "data_type": "INTEGER",
                                            "nullable": false
                                        },
                                        {
                                            "name": "Order ID",
                                            "data_type": "TEXT",
                                            "nullable": false
                                        }
                                    ],
                                    "data_preview": [
                                        {"Row ID": 1, "Order ID": "CA-2020-152156", "Sales": 261.96},
                                        {"Row ID": 2, "Order ID": "CA-2020-152156", "Sales": 731.94}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ],
            "summary": {
                "total_worksheets": 5,
                "total_dashboards": 2,
                "total_calculated_fields": 14,
                "total_parameters": 3,
                "total_data_sources": 1,
                "total_tables": 2,
                "total_rows": 9994
            }
        }
    """
    try:
        # Get migration
        migration = migration_store.get_migration(migration_id)

        if not migration:
            raise HTTPException(status_code=404, detail="Migration not found")

        # Get workbooks
        workbooks = migration_store.get_workbooks_by_migration(migration_id)

        if not workbooks:
            raise HTTPException(status_code=404, detail="No workbooks found for this migration")

        # Build comprehensive metadata for each workbook
        workbooks_metadata = []

        # Summary counters
        total_worksheets = 0
        total_dashboards = 0
        total_calculated_fields = 0
        total_parameters = 0
        total_data_sources = 0
        total_tables = 0
        total_rows = 0

        for workbook in workbooks:
            try:
                import json as _json
                # ── Load raw_model (pre-parsed JSON from tableau_extractor) ──
                model = workbook.raw_model or {}
                if isinstance(model, str):
                    try:
                        model = _json.loads(model)
                    except Exception:
                        model = {}

                # ── Build caption map for calculated field resolution ──────
                calc_map = {}
                for col in model.get("columns", []):
                    if col.get("formula"):
                        internal = col.get("internal_name", "")
                        caption = col.get("caption", internal)
                        calc_map[internal] = col

                # ── Worksheets ─────────────────────────────────────────────
                worksheets_raw = model.get("worksheets", [])
                worksheets = []
                # Build lookup sets for quick field matching
                calc_by_caption = {}
                calc_by_internal = {}
                for col in model.get("columns", []):
                    internal = col.get("internal_name", "")
                    caption  = col.get("caption") or internal
                    calc_by_caption[caption]  = col
                    calc_by_internal[internal] = col

                def _classify_field(raw_name: str):
                    import re as _re
                    is_agg = bool(_re.match(r'^[A-Z]{3,}\(', raw_name.strip()))
                    m = _re.search(r'\[([^\]]+)\]', raw_name)
                    name = m.group(1) if m else raw_name.strip("[] ")
                    col = calc_by_caption.get(name) or calc_by_internal.get(name)
                    return name, col, is_agg

                for ws in worksheets_raw:
                    ws_name   = ws.get("name", "")
                    mark_type = ws.get("mark_type", "")

                    # Collect every field reference on this worksheet's shelves
                    all_shelf_fields = []
                    all_shelf_fields += ws.get("rows", [])
                    all_shelf_fields += ws.get("cols", [])
                    for pane in ws.get("pane_encodings", []):
                        all_shelf_fields += list(pane.get("encodings", {}).values())
                    all_shelf_fields += list(ws.get("window_cards", {}).values())
                    all_shelf_fields += list(ws.get("dashboard_cards", {}).values())
                    for f in ws.get("filters", []):
                        fld = f.get("field") if isinstance(f, dict) else f
                        if fld:
                            all_shelf_fields.append(fld)

                    # Deduplicate while preserving order
                    seen_fields = set()
                    resolved_measures   = []
                    resolved_dimensions = []
                    ws_calculated_fields = []

                    for raw in all_shelf_fields:
                        if not raw:
                            continue
                        display_name, col, is_agg = _classify_field(str(raw))
                        if display_name in seen_fields:
                            continue
                        seen_fields.add(display_name)

                        calc_display_name = _norm_table(col.get("caption") or display_name) if col else _norm_table(display_name)
                        is_calc = bool(col and col.get("formula"))
                        role = col.get("role", "").lower() if col else "dimension"

                        if is_calc:
                            ws_calculated_fields.append({
                                "name":      calc_display_name,
                                "formula":   col.get("formula", ""),
                                "calc_type": col.get("formula_type", ""),
                                "datatype":  col.get("datatype", ""),
                                "role":      col.get("role", ""),
                            })
                            if role == "dimension" and not is_agg:
                                resolved_dimensions.append(calc_display_name)
                            else:
                                resolved_measures.append({
                                    "type": "calculated",
                                    "name": calc_display_name,
                                    "formula": col.get("formula", "")
                                })
                        else:
                            if is_agg or role == "measure":
                                resolved_measures.append({
                                    "type": "base_measure",
                                    "name": calc_display_name
                                })
                            else:
                                resolved_dimensions.append(calc_display_name)

                    # Deduplicate dimensions list
                    unique_dimensions = list(dict.fromkeys(resolved_dimensions))

                    # Construct axes for the frontend properly mapped as strings
                    def _format_axis(shelf_list):
                        formatted = []
                        for item in shelf_list:
                            name, col, is_agg = _classify_field(str(item))
                            label = _norm_table(col.get("caption") or name) if col else _norm_table(name)
                            formatted.append(label)
                        return ", ".join(formatted)

                    axes_dict = {
                        "rows": _format_axis(ws.get("rows", [])),
                        "columns": _format_axis(ws.get("cols", []))
                    }

                    # Infer chart type if Automatic
                    inferred_chart_type = mark_type or "Automatic"
                    if inferred_chart_type.lower() == "automatic":
                        def has_date(shelf):
                            import re
                            for item in shelf:
                                item_str = str(item).strip()
                                if re.match(r'^(YEAR|QUARTER|MONTH|DAY|WEEK|MDY|MY)\(', item_str):
                                    return True
                                name, col, is_agg = _classify_field(item_str)
                                if col and col.get('datatype') in ['date', 'datetime']:
                                    return True
                            return False

                        def count_meas_dim(shelf):
                            meas_cnt = 0
                            dim_cnt = 0
                            import re
                            for item in shelf:
                                if re.match(r'^[A-Z]{3,}\(', str(item).strip()):
                                    meas_cnt += 1
                                else:
                                    dim_cnt += 1
                            return meas_cnt, dim_cnt

                        r_meas, r_dim = count_meas_dim(ws.get("rows", []))
                        c_meas, c_dim = count_meas_dim(ws.get("cols", []))

                        if r_meas > 0 and c_meas > 0:
                            inferred_chart_type = "Scatter Plot"
                        elif (has_date(ws.get("cols", [])) and r_meas > 0) or (has_date(ws.get("rows", [])) and c_meas > 0):
                            inferred_chart_type = "Line Chart"
                        elif (r_dim > 0 and c_meas > 0) or (c_dim > 0 and r_meas > 0):
                            inferred_chart_type = "Bar Chart"
                        elif r_dim > 0 and c_dim > 0:
                            inferred_chart_type = "Text Table"
                        elif (r_meas + r_dim + c_meas + c_dim) == 0 and len(resolved_measures) > 0:
                            inferred_chart_type = "Card"
                        else:
                            inferred_chart_type = "Text Table"

                    worksheets.append({
                        "worksheet_name":    ws_name,
                        "chart_type":        inferred_chart_type,
                        "axes":              axes_dict,
                        "dimensions":        unique_dimensions,
                        "measures":          resolved_measures,
                        "calculated_fields": ws_calculated_fields,
                        # Legacy fields kept for frontend compatibility
                        "name":              ws_name,
                        "visual_type":       mark_type,
                        "mark_type":         mark_type,
                        "rows_fields":       ws.get("rows", []),
                        "columns_fields":    ws.get("cols", []),
                        "marks_fields":      ws.get("marks_fields", []),
                        "filters":           ws.get("filters", [])
                    })

                # ── Dashboards ─────────────────────────────────────────────
                dashboards_raw = model.get("dashboards", [])
                dashboards = [
                    {
                        "name": str(db.get("name", "")) if isinstance(db, dict) else str(db),
                        "worksheets_included": db.get("worksheets", []) if isinstance(db, dict) else []
                    }
                    for db in dashboards_raw
                ]

                # ── Calculated Fields (columns with formula, deduplicated) ─
                seen_calc_names = set()
                calculated_fields = []
                for col in model.get("columns", []):
                    if not col.get("formula"):
                        continue
                    name = col.get("internal_name", "")
                    if name in seen_calc_names:
                        continue
                    seen_calc_names.add(name)
                    calculated_fields.append({
                        "name": name,
                        "formula": col.get("formula", ""),
                        "calc_type": col.get("formula_type", ""),
                        "datatype": col.get("datatype", ""),
                        "role": col.get("role", ""),
                        "caption": col.get("caption") or None
                    })

                logger.info(f"Found {len(calculated_fields)} unique calculated fields from raw_model")

                # ── LOD Expressions ────────────────────────────────────────
                lod_expressions = [
                    {
                        "name": lod.get("caption", ""),
                        "lod_type": lod.get("lod_type", ""),
                        "dimensions": [],
                        "aggregation": "",
                        "formula": lod.get("formula", "")
                    }
                    for lod in model.get("lod_calculations", [])
                ]

                # ── Parameters ─────────────────────────────────────────────
                parameters = [
                    {
                        "name": p.get("name", ""),
                        "datatype": p.get("datatype", ""),
                        "current_value": str(p.get("current_value", "")),
                        "allowable_values": [
                            v.get("value", v) if isinstance(v, dict) else str(v)
                            for v in p.get("allowable_values", [])
                        ],
                        "alias": p.get("alias") or None
                    }
                    for p in model.get("parameters", [])
                ]

                # ── Hyper Table Profiling ──────────────────────────────────
                workbook_tables = []
                hyper_path_str = None

                # Primary: Use unified resolution helper
                hyper_path_str = _resolve_hyper_path(model, migration_id)

                logger.info(f"Hyper path resolved: {hyper_path_str}")

                if hyper_path_str:
                    try:
                        logger.info(f"Profiling Hyper file once for workbook: {hyper_path_str}")
                        profiler = HyperDataProfiler(hyper_path_str)
                        tables = profiler.list_tables()
                        logger.info(f"Found {len(tables)} tables to profile: {tables}")

                        for table in tables:
                            try:
                                table_unquoted = table.strip('"').replace('"."', '.')
                                logger.info(f"Profiling table: {table} (unquoted: {table_unquoted})")
                                table_profile = profiler.profile_table(table_unquoted, sample_size=100)
                                df = profiler.read_table(table_unquoted, limit=10)
                                row_count = table_profile.row_count
                                total_rows += row_count

                                # JSON-safe data preview
                                data_preview = []
                                for row in df.to_dict('records'):
                                    clean_row = {}
                                    for key, value in row.items():
                                        if pd.isna(value):
                                            clean_row[str(key)] = None
                                        elif isinstance(value, (pd.Timestamp, pd.DatetimeTZDtype)):
                                            clean_row[str(key)] = str(value)
                                        elif isinstance(value, (int, float, str, bool)):
                                            clean_row[str(key)] = value
                                        else:
                                            clean_row[str(key)] = str(value)
                                    data_preview.append(clean_row)

                                columns_info = []
                                for col_profile in table_profile.columns:
                                    data_type = str(col_profile.data_type).upper()
                                    if 'INT' in data_type:
                                        data_type = 'INTEGER'
                                    elif 'FLOAT' in data_type or 'DOUBLE' in data_type:
                                        data_type = 'REAL'
                                    elif 'OBJECT' in data_type or 'STR' in data_type:
                                        data_type = 'TEXT'
                                    elif 'BOOL' in data_type:
                                        data_type = 'BOOLEAN'
                                    elif 'DATETIME' in data_type or 'DATE' in data_type:
                                        data_type = 'DATE'
                                    columns_info.append({
                                        "name": str(col_profile.column_name),
                                        "data_type": str(data_type),
                                        "nullable": bool(col_profile.null_count > 0)
                                    })

                                display_name = _norm_table(profiler.get_clean_table_name(table))
                                workbook_tables.append({
                                    "table_name": str(table),
                                    "display_name": display_name,
                                    "row_count": int(row_count),
                                    "columns": columns_info,
                                    "column_details": columns_info,
                                    "data_preview": data_preview
                                })
                                total_tables += 1
                                logger.info(f"Successfully profiled table {table} with {len(columns_info)} columns")

                            except Exception as table_error:
                                logger.error(f"Failed to profile individual table {table}: {table_error}", exc_info=True)

                    except Exception as e:
                        logger.error(f"Failed to initialize Hyper profiler: {e}", exc_info=True)

                # ── Data Sources ───────────────────────────────────────────
                data_sources = []
                raw_datasources = model.get("datasources", []) or model.get("data_sources", [])
                all_fields = set()
                for table in workbook_tables:
                    for col in table['columns']:
                        all_fields.add(col['name'])

                if raw_datasources:
                    for ds in raw_datasources:
                        data_sources.append({
                            "name": str(ds.get("name", "")) if isinstance(ds, dict) else str(ds),
                            "connection_type": str(ds.get("connection_type", "")) if isinstance(ds, dict) else "",
                            "tables": ds.get("tables", []) if isinstance(ds, dict) else [],
                            "fields": sorted(list(all_fields)),
                            "table_details": workbook_tables
                        })
                else:
                    # Fallback: synthesize one data source entry from connection info
                    for conn in model.get("connections", []):
                        data_sources.append({
                            "name": conn.get("caption", "Tableau Data Source"),
                            "connection_type": conn.get("type", ""),
                            "tables": [],
                            "fields": sorted(list(all_fields)),
                            "table_details": workbook_tables
                        })

                # ── Counters ───────────────────────────────────────────────
                total_worksheets += len(worksheets)
                total_dashboards += len(dashboards)
                total_calculated_fields += len(calculated_fields)
                total_parameters += len(parameters)
                total_data_sources += len(data_sources)

                workbook_metadata = {
                    "workbook_id": str(workbook.workbook_id),
                    "filename": str(workbook.filename),
                    "file_path": str(workbook.file_path),
                    "worksheets": worksheets,
                    "dashboards": dashboards,
                    "calculated_fields": calculated_fields,
                    "lod_expressions": lod_expressions,
                    "parameters": parameters,
                    "data_sources": data_sources
                }

                workbooks_metadata.append(workbook_metadata)

            except Exception as e:
                logger.error(f"Failed to parse workbook {workbook.filename}: {e}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to parse workbook: {str(e)}"
                )




        # Return comprehensive metadata
        return {
            "migration_id": migration_id,
            "workbooks": workbooks_metadata,
            "summary": {
                "total_worksheets": total_worksheets,
                "total_dashboards": total_dashboards,
                "total_calculated_fields": total_calculated_fields,
                "total_lod_expressions": len([lod for wb in workbooks_metadata for lod in wb.get("lod_expressions", [])]),
                "total_parameters": total_parameters,
                "total_data_sources": total_data_sources,
                "total_tables": total_tables,
                "total_rows": total_rows
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get workbook metadata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{migration_id}/workbook-metadata/worksheets")
async def get_all_worksheets(migration_id: str):
    """
    Get only worksheet names and metadata

    Returns:
        {
            "worksheets": [
                {"name": "Sales Overview", "visual_type": "bar", "fields_used": ["Region", "Sales"]},
                {"name": "Trends", "visual_type": "line", "fields_used": ["Date", "Revenue"]}
            ]
        }
    """
    metadata = await get_comprehensive_workbook_metadata(migration_id)

    all_worksheets = []
    for wb in metadata["workbooks"]:
        for ws in wb["worksheets"]:
            # Collect all fields used in this worksheet
            fields_used = set(ws["rows_fields"] + ws["columns_fields"] + ws["marks_fields"])

            all_worksheets.append({
                "name": ws["name"],
                "visual_type": ws["visual_type"],
                "mark_type": ws["mark_type"],
                "fields_used": sorted(list(fields_used)),
                "workbook": wb["filename"]
            })

    return {"worksheets": all_worksheets}


@router.get("/{migration_id}/workbook-metadata/dashboards")
async def get_all_dashboards(migration_id: str):
    """
    Get only dashboard names

    Returns:
        {
            "dashboards": [
                {"name": "Executive Dashboard", "worksheets": ["Sales", "Trends"]},
                {"name": "Regional View", "worksheets": ["Map", "Table"]}
            ]
        }
    """
    metadata = await get_comprehensive_workbook_metadata(migration_id)

    all_dashboards = []
    for wb in metadata["workbooks"]:
        for db in wb["dashboards"]:
            all_dashboards.append({
                "name": db["name"],
                "worksheets": db["worksheets_included"],
                "workbook": wb["filename"]
            })

    return {"dashboards": all_dashboards}


@router.get("/{migration_id}/workbook-metadata/calculated-fields")
async def get_all_calculated_fields(migration_id: str):
    """
    Get only calculated field names and formulas

    Returns:
        {
            "calculated_fields": [
                {"name": "Profit Ratio", "formula": "SUM([Profit])/SUM([Sales])", "type": "measure"},
                {"name": "Customer Segment", "formula": "IF [Sales] > 1000 THEN 'High' ELSE 'Low' END", "type": "dimension"}
            ]
        }
    """
    metadata = await get_comprehensive_workbook_metadata(migration_id)

    all_calc_fields = []
    for wb in metadata["workbooks"]:
        for cf in wb["calculated_fields"]:
            all_calc_fields.append({
                "name": cf["name"],
                "formula": cf["formula"],
                "calc_type": cf["calc_type"],
                "datatype": cf["datatype"],
                "role": cf["role"],
                "workbook": wb["filename"]
            })

    return {"calculated_fields": all_calc_fields}


@router.get("/{migration_id}/workbook-metadata/tables-data")
async def get_all_tables_with_data(migration_id: str):
    """
    Get all table names, columns, and data previews

    Returns:
        {
            "tables": [
                {
                    "table_name": "Extract",
                    "row_count": 9994,
                    "columns": ["Row ID", "Order ID", "Sales", "Profit"],
                    "column_details": [...],
                    "data_preview": [...]
                }
            ]
        }
    """
    metadata = await get_comprehensive_workbook_metadata(migration_id)

    all_tables = []
    for wb in metadata["workbooks"]:
        for ds in wb["data_sources"]:
            for table_detail in ds["table_details"]:
                all_tables.append({
                    "table_name": table_detail["table_name"],
                    "row_count": table_detail["row_count"],
                    "columns": [col["name"] for col in table_detail["columns"]],
                    "column_details": table_detail["columns"],
                    "data_preview": table_detail["data_preview"],
                    "data_source": ds["name"],
                    "workbook": wb["filename"]
                })

    # Deduplicate tables by table_name (same tables appear across multiple data sources)
    seen_table_names = set()
    unique_tables = []
    for table in all_tables:
        if table["table_name"] not in seen_table_names:
            seen_table_names.add(table["table_name"])
            unique_tables.append(table)

    logger.info(f"Found {len(all_tables)} total table entries, {len(unique_tables)} unique tables")

    return {"tables": unique_tables}


@router.post("/{migration_id}/tableau-preview")
async def create_tableau_preview(
    migration_id: str,
    request: TableauPreviewRequest
):
    """
    Create a preview session from Tableau Hyper tables for relationship discovery.

    This endpoint converts selected Tableau tables into the preview session format
    used by the relationship discovery workflow. It reads DataFrames from Hyper files
    and stores them in PreviewStore for processing.

    Args:
        migration_id: The migration session identifier
        request: TableauPreviewRequest with table_names list

    Returns:
        Preview session with preview_id and file/sheet metadata

    Example:
        POST /api/v1/migration/mig_123/tableau-preview
        {
            "table_names": ["Orders", "Products", "Customers"]
        }

        Returns:
        {
            "preview_id": "prev_abc123",
            "status": "ready",
            "created_at": "2024-01-15T10:30:00Z",
            "file_count": 3,
            "files": [...]
        }
    """
    try:
        # Extract table names from request
        table_names = request.table_names

        # Validate table names
        if not table_names or len(table_names) < 1:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "INVALID_TABLE_SELECTION",
                        "message": "At least 1 table is required",
                        "details": {"min_tables": 1}
                    }
                }
            )

        # Get migration session to locate workbook files
        migration = migration_store.get_migration(migration_id)
        if not migration:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "MIGRATION_NOT_FOUND",
                        "message": f"Migration {migration_id} not found",
                        "details": {"migration_id": migration_id}
                    }
                }
            )

        # Get workbook files from migration
        workbooks = migration_store.get_workbooks_by_migration(migration_id)
        if not workbooks or len(workbooks) == 0:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "NO_WORKBOOKS_FOUND",
                        "message": "No workbooks found in migration",
                        "details": {"migration_id": migration_id}
                    }
                }
            )

        # Use first workbook (assuming single workbook migration for now)
        workbook = workbooks[0]

        # Find Hyper file paths from raw_model or fallback cache
        model = workbook.raw_model or {}
        hyper_path = _resolve_hyper_path(model, migration_id)

        if not hyper_path:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "NO_HYPER_FILES_FOUND",
                        "message": "No Hyper data files found in workbook metadata",
                        "details": {"workbook": workbook.filename}
                    }
                }
            )

        logger.info(f"Reading tables from Hyper file: {hyper_path}")

        # Read selected tables as DataFrames using HyperDataProfiler
        profiler = HyperDataProfiler(str(hyper_path))

        # Verify requested tables exist
        available_tables = profiler.list_tables()
        logger.info(f"Available tables in Hyper: {available_tables}")

        # Find matching tables (handle table name variations)
        table_map = {}  # Maps requested name -> actual table name in Hyper
        for requested_name in table_names:
            found = False
            for available_table in available_tables:
                # Match by exact name or last component (e.g., "Extract"."Extract" -> "Extract")
                table_str = str(available_table)
                if requested_name in table_str or table_str.endswith(requested_name):
                    table_map[requested_name] = available_table
                    found = True
                    break

            if not found:
                logger.warning(f"Table '{requested_name}' not found in Hyper file. Available: {available_tables}")

        if len(table_map) == 0:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "TABLES_NOT_FOUND",
                        "message": "None of the requested tables found in Hyper file",
                        "details": {
                            "requested": table_names,
                            "available": [str(t) for t in available_tables]
                        }
                    }
                }
            )

        # Generate preview ID
        preview_id = f"prev_{uuid.uuid4().hex[:12]}"

        # Create preview session
        session = preview_store.create_preview_session(
            preview_id=preview_id,
            file_count=len(table_map),
            total_duplicates_detected=0
        )

        # Read DataFrames and save to preview store
        files_preview = []

        for requested_name, hyper_table in table_map.items():
            # Read table as DataFrame
            # Strip quotes from table name for reading (same as metadata endpoint)
            table_unquoted = str(hyper_table).strip('"').replace('"."', '.')
            logger.info(f"Reading table: {hyper_table} (unquoted: {table_unquoted})")

            # OPTIMIZATION: Limit rows to 10K for preview (prevents memory explosion)
            # For 100K+ row tables, this reduces load time from 30-90 sec to 5-15 sec
            df = profiler.read_table(table_unquoted, limit=10000)

            if df is None or df.empty:
                logger.warning(f"Table {hyper_table} is empty or could not be read")
                continue

            # Clean column names (convert Name objects to strings and remove quotes)
            df.columns = [str(col).strip('"') for col in df.columns]

            # Sanitize filename for Windows (remove quotes, replace dots with underscores)
            # Keep only the last part after the final dot (table name without schema)
            safe_filename = table_unquoted.split('.')[-1] if '.' in table_unquoted else table_unquoted
            safe_filename = safe_filename.replace('"', '').replace("'", "")

            logger.info(f"Using safe filename: {safe_filename}")

            # Create dummy file content (not needed for Tableau, but required by schema)
            # We'll store DataFrames directly via pickle
            preview_file = preview_store.save_preview_file(
                preview_id=preview_id,
                original_filename=f"{safe_filename}.hyper",
                file_content=b"",  # Empty content, DataFrame is stored in pickle
                df=df,
                metadata={
                    "source": "tableau",
                    "table_name": str(hyper_table),
                    "migration_id": migration_id,
                    "workbook": workbook.filename
                }
            )

            # Build file preview response (use original requested name for display)
            files_preview.append({
                "file_id": preview_file.file_id,
                "filename": safe_filename,  # Use safe filename for display too
                "row_count": len(df),
                "column_count": len(df.columns),
                "columns": [
                    {
                        "name": str(col),
                        "type": str(df[col].dtype),
                        "null_count": int(df[col].isnull().sum()),
                        "is_duplicate_candidate": False
                    }
                    for col in df.columns
                ],
                "duplicate_groups": []
            })

            logger.info(f"Saved table '{safe_filename}' to preview {preview_id} ({len(df)} rows, {len(df.columns)} cols)")

        # Return preview response (matching Excel workflow format)
        from datetime import datetime
        return {
            "preview_id": preview_id,
            "status": "ready",
            "created_at": datetime.utcnow(),
            "file_count": len(files_preview),
            "files": files_preview,
            "total_duplicates_detected": 0,
            "message": f"Preview created successfully with {len(files_preview)} tables"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create Tableau preview: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "TABLEAU_PREVIEW_FAILED",
                    "message": "Failed to create Tableau preview",
                    "details": str(e)
                }
            }
        )


@router.get("/{migration_id}/workbook-metadata/model-intelligence")
async def get_model_intelligence(migration_id: str):
    """
    Returns Page 2 table metadata: classifications, quality, column details.
    Reads Hyper files from raw_model['hyper_files'] (set by orchestrator Phase 1).
    """
    try:
        migration = migration_store.get_migration(migration_id)
        if not migration:
            raise HTTPException(status_code=404, detail="Migration not found")

        workbooks = migration_store.get_workbooks_by_migration(migration_id)
        if not workbooks:
            raise HTTPException(status_code=404, detail="No workbooks found for migration")

        result = {
            "tables": [],
            "summary": {
                "total_tables": 0,
                "total_rows": 0,
                "fact_tables": 0,
                "dimension_tables": 0
            }
        }

        for workbook in workbooks:
            model = workbook.raw_model or {}
            hyper_path_str = _resolve_hyper_path(model, migration_id)

            if not hyper_path_str:
                logger.warning(f"No hyper_path resolved for {workbook.filename}")
                continue

            profiler = HyperDataProfiler(str(hyper_path_str))
            tables = profiler.list_tables()

            for table in tables:
                table_unquoted = str(table).strip('"').replace('"."', '.')
                clean_name = profiler.get_clean_table_name(table)

                try:
                    profile = profiler.profile_table(table_unquoted, sample_size=10000)
                    row_count = profile.row_count
                    columns = profile.columns
                    numeric_cols = [c for c in columns if c.data_type in ["int64", "float64"]]
                    numeric_density = len(numeric_cols) / len(columns) if columns else 0

                    if row_count > 100000 and numeric_density > 0.5:
                        classification, confidence = "FACT", 95
                        reasoning = f"High numeric density ({len(numeric_cols)} cols) and large row count"
                    elif row_count < 10000 and numeric_density < 0.3:
                        classification, confidence = "DIMENSION", 98
                        reasoning = f"Low numeric density ({len(numeric_cols)} cols) and small row count"
                    else:
                        classification, confidence = "DIMENSION", 70
                        reasoning = f"Mixed (rows: {row_count}, numeric: {len(numeric_cols)})"

                    duplicate_count = profiler.detect_duplicates(table_unquoted, sample_size=10000)
                    duplicate_rate = (duplicate_count / row_count * 100) if row_count > 0 else 0
                    quality_status = "good" if duplicate_rate < 1 else "warning"

                    pk_column, pk_uniqueness = None, 0
                    for col in columns:
                        if col.cardinality >= 0.99:
                            pk_column = str(col.column_name)
                            pk_uniqueness = round(col.cardinality * 100, 1)
                            break

                    column_details = [{
                        "name": str(col.column_name),
                        "data_type": str(col.data_type),
                        "nullable": col.null_count > 0,
                        "cardinality": round(col.cardinality * 100, 1) if hasattr(col, "cardinality") else 0
                    } for col in columns]

                    result["tables"].append({
                        "table_name": clean_name,
                        "row_count": row_count,
                        "column_count": len(columns),
                        "column_details": column_details,
                        "classification": classification,
                        "confidence_score": confidence,
                        "numeric_columns": len(numeric_cols),
                        "reasoning": reasoning,
                        "duplicate_count": duplicate_count,
                        "duplicate_rate": round(duplicate_rate, 2),
                        "status": quality_status,
                        "potential_primary_key": pk_column,
                        "pk_uniqueness": pk_uniqueness
                    })

                    result["summary"]["total_rows"] += row_count
                    if classification == "FACT":
                        result["summary"]["fact_tables"] += 1
                    else:
                        result["summary"]["dimension_tables"] += 1

                    logger.info(f"Profiled '{clean_name}' - {classification} ({confidence}%)")

                except Exception as table_error:
                    logger.error(f"Failed to profile table {table}: {table_error}")
                    continue

        result["summary"]["total_tables"] = len(result["tables"])
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get model intelligence: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "MODEL_INTELLIGENCE_FAILED", "message": str(e)}}
        )
