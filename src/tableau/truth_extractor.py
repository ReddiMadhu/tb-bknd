"""Truth Map Extractor - Extract ground truth from Tableau Hyper files for validation"""
import json
import re
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from pathlib import Path
from loguru import logger

try:
    from tableauhyperapi import HyperProcess, Telemetry, Connection, CreateMode
    HYPER_AVAILABLE = True
except ImportError:
    HYPER_AVAILABLE = False
    logger.warning("tableauhyperapi not available - using DuckDB fallback")

import duckdb


@dataclass
class TruthSlice:
    """A single test slice with ground truth value"""
    dimensions: Dict[str, Any]  # e.g., {"Region": "East", "Year": 2024}
    truth_value: Optional[float]
    slice_key: str  # Composite key for matching


class TruthMapExtractor:
    """
    Extract ground truth from Tableau data sources for validation

    Design:
    1. Query Hyper file with same dimensions as DAX test
    2. Generate "Truth Map" - expected results for test slices
    3. Support both Hyper API and DuckDB fallback
    4. Handle NULL values and edge cases
    """

    def __init__(self, use_hyper: bool = HYPER_AVAILABLE):
        """
        Initialize truth extractor

        Args:
            use_hyper: Use Hyper API if available, otherwise DuckDB
        """
        self.use_hyper = use_hyper and HYPER_AVAILABLE
        # Cache: hyper_path → {raw_table_name: set(lowercase_col_names)}
        # Built once per hyper file, reused for all formula scorings
        self._table_cols_cache: Dict[str, Dict[str, set]] = {}
        logger.info(f"Truth Map Extractor initialized (Hyper: {self.use_hyper})")

    # ============================================
    # Main Extraction Methods
    # ============================================

    def extract_truth_map(
        self,
        data_source: str,
        table_name: str,
        calculation: str,
        dimensions: List[str],
        filters: Optional[List[str]] = None,
        limit: int = 1000
    ) -> Dict[str, TruthSlice]:
        """
        Extract ground truth from data source

        Args:
            data_source: Path to .hyper file or DuckDB-compatible file
            table_name: Table/schema name (e.g., "Extract" or "public.Orders")
            calculation: SQL expression for measure (e.g., 'SUM("Sales")')
            dimensions: List of dimension columns to group by
            filters: Optional WHERE conditions (e.g., ['Category = "Tech"'])
            limit: Max slices to extract (prevent memory issues)

        Returns:
            Truth map keyed by composite dimension string

        Example:
            >>> extractor = TruthMapExtractor()
            >>> truth = extractor.extract_truth_map(
            ...     "data.hyper",
            ...     "Extract",
            ...     'SUM("Sales")',
            ...     ["Region", "Year"]
            ... )
            >>> # Returns: {"East|2024": TruthSlice(...), "West|2024": TruthSlice(...)}
        """
        if self.use_hyper:
            return self._extract_from_hyper(
                data_source, table_name, calculation, dimensions, filters, limit
            )
        else:
            return self._extract_from_duckdb(
                data_source, table_name, calculation, dimensions, filters, limit
            )

    # ============================================
    # Hyper API Implementation
    # ============================================

    def _extract_from_hyper(
        self,
        hyper_path: str,
        table_name: str,
        calculation: str,
        dimensions: List[str],
        filters: Optional[List[str]],
        limit: int
    ) -> Dict[str, TruthSlice]:
        """Extract using Tableau Hyper API with auto table-detection."""
        if not HYPER_AVAILABLE:
            raise Exception("tableauhyperapi not installed - use DuckDB fallback")

        # Convert Tableau formula to SQL (strips (TableName) qualifiers automatically)
        sql_calculation = self._tableau_to_sql(calculation)

        # Auto-detect the best-matching Hyper table for this formula's columns
        best_table = self._find_best_table(hyper_path, sql_calculation)
        resolved_table = best_table if best_table else table_name
        if resolved_table != table_name:
            logger.info(f"Auto-selected table: {resolved_table} (requested: {table_name})")

        # ── Calculated-field guard ────────────────────────────────────────────
        # Some formulas reference OTHER calculated fields (e.g. Calculation_175…)
        # that are computed by Tableau at runtime and never stored in any Hyper
        # table. Querying for them always fails. Detect this early and skip.
        #
        # Two signals mark a reference as a calculated field, not a raw column:
        #   1. Name matches Tableau's internal pattern  Calculation_\d+
        #   2. Name is not present in ANY table's physical column set (cache)
        all_cols_in_file: set = set()
        for cols in self._table_cols_cache.get(hyper_path, {}).values():
            all_cols_in_file |= cols

        quoted_refs = set(re.findall(r'"([^"]+)"', sql_calculation))
        calc_pattern = re.compile(r'^Calculation_\d+$')
        phantom_cols = {
            c for c in quoted_refs
            if calc_pattern.match(c)                    # Tableau internal name
            or (len(c) > 3 and c.lower() not in all_cols_in_file
                and not c[0].islower())                  # upper-case but not in any table
        }

        if phantom_cols:
            logger.warning(
                f"⚠️  Skipping truth extraction — formula references intermediate "
                f"calculated fields not stored in raw data: {phantom_cols}"
            )
            return {}
        # ─────────────────────────────────────────────────────────────────────

        # Build SQL query against the resolved table
        query = self._build_sql_query(resolved_table, sql_calculation, dimensions, filters, limit)

        truth_map = {}

        try:
            try:
                telemetry = Telemetry.DO_NOT_SEND_USAGE_DATA
            except AttributeError:
                telemetry = Telemetry.SEND_USAGE_DATA_TO_TABLEAU

            with HyperProcess(telemetry=telemetry) as hyper:
                with Connection(endpoint=hyper.endpoint, database=hyper_path) as connection:
                    result = connection.execute_list_query(query)

                    for row in result:
                        dim_values = {dimensions[i]: row[i] for i in range(len(dimensions))}
                        truth_value = float(row[-1]) if row[-1] is not None else None
                        slice_key = self._make_slice_key(dim_values)
                        truth_map[slice_key] = TruthSlice(
                            dimensions=dim_values,
                            truth_value=truth_value,
                            slice_key=slice_key
                        )

            logger.info(f"✅ Extracted {len(truth_map)} truth slices from Hyper")
            return truth_map

        except Exception as e:
            logger.error(f"Hyper extraction failed: {e}")
            logger.info("Falling back to DuckDB...")
            return self._extract_from_duckdb(
                hyper_path, resolved_table, calculation, dimensions, filters, limit
            )


    # ============================================
    # DuckDB Fallback Implementation
    # ============================================

    def _extract_from_duckdb(
        self,
        data_source: str,
        table_name: str,
        calculation: str,
        dimensions: List[str],
        filters: Optional[List[str]],
        limit: int
    ) -> Dict[str, TruthSlice]:
        """Extract using DuckDB — for Hyper files loads data via HyperDataProfiler."""

        sql_calculation = self._tableau_to_sql(calculation)
        truth_map = {}

        try:
            con = duckdb.connect(database=':memory:')

            if data_source.endswith('.hyper'):
                # For Hyper files, load the matching table into DuckDB as "data"
                # (DuckDB cannot query Hyper schema-qualified names directly)
                try:
                    from src.tableau.hyper_profiler import HyperDataProfiler
                    profiler = HyperDataProfiler(data_source)
                    # Resolve best table (same logic as Hyper path)
                    best_raw = table_name if table_name else profiler.list_tables()[0]
                    unquoted = best_raw.replace('"', '').replace("'", '')
                    df = profiler.read_table(unquoted)
                    con.execute("CREATE TABLE data AS SELECT * FROM df")
                    # Query from "data" (DuckDB in-memory table)
                    query = self._build_sql_query('data', sql_calculation, dimensions, filters, limit)
                    logger.info(f"DuckDB fallback: loaded {len(df)} rows from '{unquoted}'")
                except Exception as load_err:
                    logger.error(f"DuckDB Hyper load failed: {load_err}")
                    return {}
            elif data_source.endswith('.parquet'):
                con.execute(f"CREATE TABLE data AS SELECT * FROM '{data_source}'")
                query = self._build_sql_query('data', sql_calculation, dimensions, filters, limit)
            elif data_source.endswith('.csv'):
                con.execute(f"CREATE TABLE data AS SELECT * FROM read_csv_auto('{data_source}')")
                query = self._build_sql_query('data', sql_calculation, dimensions, filters, limit)
            else:
                query = self._build_sql_query(table_name, sql_calculation, dimensions, filters, limit)

            result = con.execute(query).fetchall()

            for row in result:
                dim_values = {dimensions[i]: row[i] for i in range(len(dimensions))}
                truth_value = float(row[-1]) if row[-1] is not None else None
                slice_key = self._make_slice_key(dim_values)
                truth_map[slice_key] = TruthSlice(
                    dimensions=dim_values,
                    truth_value=truth_value,
                    slice_key=slice_key
                )

            con.close()
            logger.info(f"✅ Extracted {len(truth_map)} truth slices from DuckDB")
            return truth_map

        except Exception as e:
            logger.error(f"DuckDB extraction failed: {e}")
            return {}

    # ============================================
    # SQL Query Builder
    # ============================================

    def _build_sql_query(
        self,
        table_name: str,
        calculation: str,
        dimensions: List[str],
        filters: Optional[List[str]],
        limit: int
    ) -> str:
        """
        Build SQL query for truth extraction

        Example output:
            SELECT "Region", "Year", SUM("Sales") as truth_value
            FROM "Extract"
            LIMIT 1000
        """
        # Ensure table name is properly quoted
        # If already quoted, use as-is; otherwise quote it
        if not (table_name.startswith('"') or table_name.startswith("'")):
            quoted_table = f'"{table_name}"'
        else:
            quoted_table = table_name

        # Handle empty dimensions (simple aggregation with no grouping)
        if not dimensions or len(dimensions) == 0:
            query = f"""
                SELECT {calculation} as truth_value
                FROM {quoted_table}
            """

            if filters:
                where_clause = " AND ".join(filters)
                query += f"\n                WHERE {where_clause}"

            query += f"\n                LIMIT {limit}"

            return query.strip()

        # Quote column names to handle spaces/special chars
        select_dims = ", ".join([f'"{d}"' for d in dimensions])
        group_by_dims = ", ".join([f'"{d}"' for d in dimensions])
        order_by_dims = ", ".join([f'"{d}"' for d in dimensions])

        # Build query
        query = f"""
            SELECT {select_dims}, {calculation} as truth_value
            FROM {quoted_table}
        """

        if filters:
            where_clause = " AND ".join(filters)
            query += f"\n            WHERE {where_clause}"

        query += f"""
            GROUP BY {group_by_dims}
            ORDER BY {order_by_dims}
            LIMIT {limit}
        """

        return query.strip()

    # ============================================
    # Utility Methods
    # ============================================

    def _make_slice_key(self, dimensions: Dict[str, Any]) -> str:
        """
        Create composite key from dimension values

        Args:
            dimensions: {"Region": "East", "Year": 2024}

        Returns:
            "East|2024" or "total" if no dimensions
        """
        if not dimensions:
            return "total"  # Single row for aggregate with no grouping

        # Sort by key for consistency
        sorted_dims = sorted(dimensions.items())
        return "|".join([str(v) for k, v in sorted_dims])

    def _tableau_to_sql(self, tableau_formula: str) -> str:
        """
        Convert Tableau calculation syntax to SQL

        Handles common patterns:
        - IF-THEN-ELSE → CASE WHEN
        - [Field] → "Field"
        - Basic aggregations (SUM, AVG, etc.)
        - Strips Tableau (TableName) qualifiers: "col (Invoice)" → "col"

        Args:
            tableau_formula: Tableau calculation syntax

        Returns:
            SQL-compatible expression
        """
        sql = tableau_formula.strip()

        # Pattern 1: Field references [Field Name] → "Field Name"
        # Do this FIRST before IF conversion
        sql = re.sub(r'\[([^\]]+)\]', r'"\1"', sql)

        # Pattern 2: Strip Tableau table-qualifier suffixes from column references
        # Tableau uses "column (TableName)" to disambiguate cross-source fields,
        # but Hyper stores only the bare column name. Strip the " (TableName)" part.
        # e.g. "income_class (Invoice)" → "income_class"
        #      "Amount (Fees)"         → "Amount"
        sql = re.sub(r'"([^"]+)\s+\([^)]+\)"', r'"\1"', sql)

        # Pattern 3: IF-THEN-ELSE → CASE WHEN
        def convert_if_to_case(match):
            condition = match.group(1).strip()
            then_value = match.group(2).strip()
            else_value = match.group(3).strip() if match.group(3) else 'NULL'
            return f'CASE WHEN {condition} THEN {then_value} ELSE {else_value} END'

        sql = re.sub(
            r'\bIF\s+(.+?)\s*THEN\s*(.+?)(?:\s*ELSE\s*(.+?))?\s*END\b',
            convert_if_to_case,
            sql,
            flags=re.IGNORECASE | re.DOTALL
        )

        # Pattern 4: Aggregation functions — work the same in Tableau and SQL
        # SUM([Sales]) is already converted to SUM("Sales") by pattern 1

        logger.info(f"🔄 Converted Tableau formula to SQL:\n  IN:  {tableau_formula}\n  OUT: {sql}")
        return sql

    # ============================================
    # Table Auto-Detection
    # ============================================

    def _find_best_table(
        self,
        hyper_path: str,
        sql_calculation: str
    ) -> Optional[str]:
        """
        Scan all tables in a Hyper file and return the raw table name whose
        columns best match the column references in sql_calculation.

        Uses read_table(limit=1) to get column names — cached per hyper_path
        so column maps are built once and reused across all 21+ formula scorings.
        """
        try:
            from src.tableau.hyper_profiler import HyperDataProfiler
            profiler = HyperDataProfiler(hyper_path)
            all_tables = profiler.list_tables()

            if not all_tables:
                return None

            # Build column map for this hyper file once, then cache it
            if hyper_path not in self._table_cols_cache:
                cache: Dict[str, set] = {}
                for raw_table in all_tables:
                    try:
                        unquoted = raw_table.replace('"', '').replace("'", '')
                        df = profiler.read_table(unquoted, limit=1)
                        cache[raw_table] = {c.lower() for c in df.columns}
                    except Exception as e:
                        logger.debug(f"Cache build failed for {raw_table}: {e}")
                        cache[raw_table] = set()
                self._table_cols_cache[hyper_path] = cache
                logger.debug(f"Built column cache for {len(cache)} tables in {hyper_path}")

            table_cols_map = self._table_cols_cache[hyper_path]

            # Extract double-quoted identifiers from the SQL formula
            col_refs = set(re.findall(r'"([^"]+)"', sql_calculation))
            # Filter out string literals; keep actual column-like names
            col_refs = {
                c for c in col_refs
                if len(c) > 3 and (
                    "_" in c
                    or any(ch.isupper() for ch in c)
                    or len(c) > 10
                )
            }

            # Score each table: count how many formula columns it contains
            best_table = None
            best_score = -1
            for raw_table, cols in table_cols_map.items():
                score = sum(1 for c in col_refs if c.lower() in cols)
                if score > best_score:
                    best_score = score
                    best_table = raw_table

            if best_table and best_score > 0:
                return best_table

            return all_tables[0]  # Fallback to first table

        except Exception as e:
            logger.debug(f"Table auto-detection failed: {e}")
            return None


    def save_truth_map(self, truth_map: Dict[str, TruthSlice], output_path: str):
        """Save truth map to JSON file for debugging"""
        serializable = {
            key: {
                "dimensions": slice.dimensions,
                "truth_value": slice.truth_value,
                "slice_key": slice.slice_key
            }
            for key, slice in truth_map.items()
        }

        with open(output_path, 'w') as f:
            json.dump(serializable, f, indent=2)

        logger.info(f"Truth map saved to {output_path}")

    def extract_sample_slices(
        self,
        data_source: str,
        table_name: str,
        calculation: str,
        dimensions: List[str],
        sample_size: int = 10
    ) -> Dict[str, TruthSlice]:
        """
        Extract a small sample for quick validation testing

        Useful for:
        - Initial debugging
        - Smoke tests
        - Demo scenarios
        """
        return self.extract_truth_map(
            data_source, table_name, calculation, dimensions,
            filters=None, limit=sample_size
        )


# ============================================
# Convenience Functions
# ============================================

def extract_tableau_truth(
    hyper_path: str,
    calculation: str,
    dimensions: List[str],
    table_name: str = "Extract"
) -> Dict[str, float]:
    """
    Simple convenience function for basic truth extraction

    Args:
        hyper_path: Path to .hyper file
        calculation: SQL expression (e.g., 'SUM("Sales") / NULLIF(SUM("Quantity"), 0)')
        dimensions: Grouping columns
        table_name: Table name (default "Extract" for Tableau extracts)

    Returns:
        Simple dict mapping composite keys to values
    """
    extractor = TruthMapExtractor()
    truth_map = extractor.extract_truth_map(
        hyper_path, table_name, calculation, dimensions
    )

    return {key: slice.truth_value for key, slice in truth_map.items()}


if __name__ == "__main__":
    # Example usage
    print("Truth Map Extractor - Example Usage\n")

    # Mock example (replace with real .hyper file)
    EXAMPLE_HYPER = "superstore.hyper"

    if Path(EXAMPLE_HYPER).exists():
        extractor = TruthMapExtractor()

        truth = extractor.extract_truth_map(
            data_source=EXAMPLE_HYPER,
            table_name='"Extract"."Extract"',
            calculation='SUM("Sales")',
            dimensions=["Region", "Category"],
            limit=50
        )

        print(f"Extracted {len(truth)} test slices:")
        for key, slice in list(truth.items())[:5]:
            print(f"  {slice.dimensions} = ${slice.truth_value:,.2f}")
    else:
        print(f"Example file {EXAMPLE_HYPER} not found")
        print("This module requires a Tableau .hyper file to test")
