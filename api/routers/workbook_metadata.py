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
from src.tableau.twb_parser import TableauTWBParser
from src.tableau.hyper_profiler import HyperDataProfiler

router = APIRouter(prefix="/api/v1/migration", tags=["workbook-metadata"])

# Initialize stores
migration_store = MigrationStore()
preview_store = PreviewStore()


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
                parser = TableauTWBParser(workbook.file_path)

                # Get counts only (fast)
                worksheets = parser.parse_worksheets()
                dashboards = parser.parse_dashboards()
                calculated_fields = parser.parse_calculated_fields()
                parameters = parser.parse_parameters()
                data_sources = parser.parse_data_sources()

                # Get table names (no profiling yet)
                table_names = []
                if parser.hyper_files:
                    try:
                        profiler = HyperDataProfiler(str(parser.hyper_files[0]))
                        table_names = profiler.list_tables()
                    except Exception as e:
                        logger.warning(f"Could not list tables: {e}")

                workbooks_summary.append({
                    "workbook_id": workbook.workbook_id,
                    "filename": workbook.filename,
                    "worksheet_count": len(worksheets),
                    "dashboard_count": len(dashboards),
                    "calculated_field_count": len(calculated_fields),
                    "parameter_count": len(parameters),
                    "data_source_count": len(data_sources),
                    "table_count": len(table_names),
                    "table_names": table_names
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

        parser = TableauTWBParser(workbook.file_path)
        worksheets_raw = parser.parse_worksheets()

        worksheets = [
            {
                "name": str(ws.name) if ws.name else "",
                "visual_type": str(ws.visual_type.value) if ws.visual_type else "",
                "mark_type": str(ws.mark_type) if ws.mark_type else "",
                "rows_fields": [str(f) for f in ws.rows_fields] if ws.rows_fields else [],
                "columns_fields": [str(f) for f in ws.columns_fields] if ws.columns_fields else [],
                "marks_fields": [str(f) for f in ws.marks_fields] if ws.marks_fields else [],
                "filters": [str(f) for f in ws.filters] if ws.filters else []
            }
            for ws in worksheets_raw
        ]

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

        parser = TableauTWBParser(workbook.file_path)
        calculated_fields_raw = parser.parse_calculated_fields()

        # Deduplicate
        seen_calc_names = set()
        unique_calculated_fields = []
        for cf in calculated_fields_raw:
            if cf.name not in seen_calc_names:
                seen_calc_names.add(cf.name)
                unique_calculated_fields.append(cf)

        calculated_fields = [
            {
                "name": str(cf.name) if cf.name else "",
                "formula": str(cf.formula) if cf.formula else "",
                "calc_type": str(cf.calc_type) if cf.calc_type else "",
                "datatype": str(cf.datatype) if cf.datatype else "",
                "role": str(cf.role) if cf.role else "",
                "caption": str(cf.caption) if cf.caption else None
            }
            for cf in unique_calculated_fields
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

        parser = TableauTWBParser(workbook.file_path)

        if not parser.hyper_files:
            raise HTTPException(status_code=404, detail="No Hyper files found")

        profiler = HyperDataProfiler(str(parser.hyper_files[0]))

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
                # Parse TWB file
                parser = TableauTWBParser(workbook.file_path)

                # Extract worksheets
                worksheets_raw = parser.parse_worksheets()
                worksheets = [
                    {
                        "name": str(ws.name) if ws.name else "",
                        "visual_type": str(ws.visual_type.value) if ws.visual_type else "",
                        "mark_type": str(ws.mark_type) if ws.mark_type else "",
                        "rows_fields": [str(f) for f in ws.rows_fields] if ws.rows_fields else [],
                        "columns_fields": [str(f) for f in ws.columns_fields] if ws.columns_fields else [],
                        "marks_fields": [str(f) for f in ws.marks_fields] if ws.marks_fields else [],
                        "filters": [str(f) for f in ws.filters] if ws.filters else []
                    }
                    for ws in worksheets_raw
                ]

                # Extract dashboards
                dashboards_raw = parser.parse_dashboards()
                dashboards = [
                    {
                        "name": str(db.name) if db.name else "",
                        "worksheets_included": [str(w) for w in db.worksheets] if db.worksheets else []
                    }
                    for db in dashboards_raw
                ]

                # Extract calculated fields
                calculated_fields_raw = parser.parse_calculated_fields()

                # Deduplicate calculated fields by name (same field often appears in multiple data sources)
                seen_calc_names = set()
                unique_calculated_fields = []
                for cf in calculated_fields_raw:
                    if cf.name not in seen_calc_names:
                        seen_calc_names.add(cf.name)
                        unique_calculated_fields.append(cf)

                logger.info(f"Found {len(calculated_fields_raw)} calculated fields, {len(unique_calculated_fields)} unique")

                calculated_fields = [
                    {
                        "name": str(cf.name) if cf.name else "",
                        "formula": str(cf.formula) if cf.formula else "",
                        "calc_type": str(cf.calc_type) if cf.calc_type else "",
                        "datatype": str(cf.datatype) if cf.datatype else "",
                        "role": str(cf.role) if cf.role else "",
                        "caption": str(cf.caption) if cf.caption else None
                    }
                    for cf in unique_calculated_fields
                ]

                # Extract LOD expressions
                lod_expressions_raw = parser.parse_lod_expressions()
                lod_expressions = [
                    {
                        "name": str(lod.name) if lod.name else "",
                        "lod_type": str(lod.lod_type) if lod.lod_type else "",
                        "dimensions": [str(d) for d in lod.dimensions] if lod.dimensions else [],
                        "aggregation": str(lod.aggregation) if lod.aggregation else "",
                        "formula": str(lod.formula) if lod.formula else ""
                    }
                    for lod in lod_expressions_raw
                ]

                # Extract parameters
                parameters_raw = parser.parse_parameters()
                parameters = [
                    {
                        "name": str(param.name) if param.name else "",
                        "datatype": str(param.datatype) if param.datatype else "",
                        "current_value": str(param.current_value) if param.current_value else "",
                        "allowable_values": [str(v) for v in param.allowable_values] if param.allowable_values else [],
                        "alias": str(param.alias) if param.alias else None
                    }
                    for param in parameters_raw
                ]

                # Extract data sources
                data_sources_raw = parser.parse_data_sources()
                data_sources = []

                # Profile tables ONCE per workbook (not per data source)
                # All data sources in a workbook typically share the same Hyper extract
                workbook_tables = []
                hyper_files = parser.hyper_files

                if hyper_files and len(hyper_files) > 0:
                    hyper_path = hyper_files[0]  # Use first Hyper file
                    hyper_path_str = str(hyper_path)

                    try:
                        logger.info(f"Profiling Hyper file once for workbook: {hyper_path_str}")
                        profiler = HyperDataProfiler(hyper_path_str)
                        tables = profiler.list_tables()

                        logger.info(f"Found {len(tables)} tables to profile: {tables}")

                        for table in tables:
                            try:
                                # Strip quotes from table name for profiling
                                table_unquoted = table.strip('"').replace('"."', '.')

                                logger.info(f"Profiling table: {table} (unquoted: {table_unquoted})")

                                # Profile the table
                                table_profile = profiler.profile_table(table_unquoted, sample_size=100)

                                # Get row count and data preview
                                df = profiler.read_table(table_unquoted, limit=10)
                                row_count = table_profile.row_count
                                total_rows += row_count

                                # Get data preview (10 rows) and convert to JSON-safe format
                                data_preview_raw = df.to_dict('records')

                                # Ensure all values are JSON-serializable
                                data_preview = []
                                for row in data_preview_raw:
                                    clean_row = {}
                                    for key, value in row.items():
                                        # Convert pandas types to Python types
                                        if pd.isna(value):
                                            clean_row[str(key)] = None
                                        elif isinstance(value, (pd.Timestamp, pd.DatetimeTZDtype)):
                                            clean_row[str(key)] = str(value)
                                        elif isinstance(value, (int, float, str, bool)):
                                            clean_row[str(key)] = value
                                        else:
                                            clean_row[str(key)] = str(value)
                                    data_preview.append(clean_row)

                                # Extract column info from TableProfile
                                columns_info = []
                                for col_profile in table_profile.columns:
                                    # Map pandas dtype to SQL type
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

                                table_detail = {
                                    "table_name": str(table),
                                    "row_count": int(row_count),
                                    "columns": columns_info,
                                    "column_details": columns_info,  # Frontend expects this field name
                                    "data_preview": data_preview
                                }

                                workbook_tables.append(table_detail)
                                total_tables += 1

                                logger.info(f"Successfully profiled table {table} with {len(columns_info)} columns")

                            except Exception as table_error:
                                logger.error(f"Failed to profile individual table {table}: {table_error}", exc_info=True)
                                # Continue to next table

                    except Exception as e:
                        logger.error(f"Failed to initialize Hyper profiler: {e}", exc_info=True)
                        # Continue without data details

                # Now process each data source (just metadata, not table profiling)
                for ds in data_sources_raw:
                    # Collect all field names from workbook tables
                    all_fields = set()
                    for table in workbook_tables:
                        for col in table['columns']:
                            all_fields.add(col['name'])

                    data_source = {
                        "name": str(ds.name) if ds.name else "",
                        "connection_type": str(ds.connection_type) if ds.connection_type else "",
                        "tables": [str(t) for t in ds.tables] if ds.tables else [],
                        "fields": sorted(list(all_fields)),  # All column names
                        "table_details": workbook_tables  # Reference workbook-level tables
                    }

                    data_sources.append(data_source)

                # Update counters
                total_worksheets += len(worksheets)
                total_dashboards += len(dashboards)
                total_calculated_fields += len(calculated_fields)
                total_parameters += len(parameters)
                total_data_sources += len(data_sources)

                # Build workbook metadata (ensure all values are JSON-serializable)
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
        twbx_path = Path(workbook.file_path)

        # Parse workbook to get Hyper files (parsing happens in __init__)
        parser = TableauTWBParser(twbx_path)

        if not parser.hyper_files or len(parser.hyper_files) == 0:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "NO_HYPER_FILES_FOUND",
                        "message": "No Hyper data files found in workbook",
                        "details": {"workbook": workbook.filename}
                    }
                }
            )

        # Use first Hyper file
        hyper_path = parser.hyper_files[0]
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

            df = profiler.read_table(table_unquoted)

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
