"""Tableau Hyper Data Profiler - Extract and profile data from Tableau extracts"""
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass
import pandas as pd
from loguru import logger

try:
    from tableauhyperapi import HyperProcess, Telemetry, Connection, TableDefinition, SqlType
except ImportError:
    logger.warning("tableauhyperapi not installed. Hyper profiler will use fallback DuckDB reader.")
    HyperProcess = None


@dataclass
class ColumnProfile:
    """Column data profile"""
    column_name: str
    data_type: str
    row_count: int
    null_count: int
    null_percent: float
    distinct_count: int
    cardinality: float
    sample_values: List[Any]
    is_numeric: bool
    min_value: Optional[Any] = None
    max_value: Optional[Any] = None
    mean_value: Optional[float] = None


@dataclass
class TableProfile:
    """Table data profile"""
    table_name: str
    row_count: int
    column_count: int
    columns: List[ColumnProfile]
    sample_data: pd.DataFrame
    primary_key_candidates: List[str]


class HyperDataProfiler:
    """
    Production-grade Tableau Hyper data profiler

    Capabilities:
    - Read Hyper extract files (Tableau's columnar format)
    - Profile data for validation context
    - Generate test slices for validation
    - Execute Tableau-like formulas against data
    - Extract ground truth for DAX comparison
    """

    def __init__(self, hyper_file_path: str):
        """
        Initialize profiler with a Hyper file

        Args:
            hyper_file_path: Path to .hyper file
        """
        self.hyper_file = Path(hyper_file_path)
        if not self.hyper_file.exists():
            raise FileNotFoundError(f"Hyper file not found: {hyper_file_path}")

        self.tables: List[str] = []
        self.profiles: Dict[str, TableProfile] = {}

        # Detect if we can use native Hyper API or fallback to DuckDB
        self.use_hyper_api = HyperProcess is not None

        if not self.use_hyper_api:
            logger.info("Using DuckDB fallback for Hyper file reading")
            self._init_duckdb_connection()

        self._discover_tables()

    def _init_duckdb_connection(self):
        """Initialize DuckDB connection as fallback"""
        try:
            import duckdb
            self.duckdb_conn = duckdb.connect()
            # DuckDB can read Parquet and other formats; may need conversion
            logger.debug("DuckDB connection initialized")
        except ImportError:
            raise ImportError("Neither tableauhyperapi nor duckdb is available. Install one of them.")

    # ============================================
    # Table Discovery
    # ============================================

    def _discover_tables(self):
        """Discover all tables in the Hyper file"""
        if self.use_hyper_api:
            self._discover_tables_hyper()
        else:
            self._discover_tables_duckdb()

        logger.info(f"Discovered {len(self.tables)} tables in Hyper file")

    def _discover_tables_hyper(self):
        """Discover tables using native Hyper API"""
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(hyper.endpoint, str(self.hyper_file)) as connection:
                # Query catalog for table names
                schema_names = connection.catalog.get_schema_names()

                for schema in schema_names:
                    table_names = connection.catalog.get_table_names(schema)
                    for table in table_names:
                        full_table_name = f"{schema}.{table.name}"
                        self.tables.append(full_table_name)
                        logger.debug(f"Found table: {full_table_name}")

    def _discover_tables_duckdb(self):
        """Discover tables using DuckDB (fallback)"""
        # For DuckDB fallback, we'd need to convert Hyper to readable format
        # This is a placeholder - in production, use hyper-db Python package
        logger.warning("DuckDB table discovery not fully implemented. Using mock tables.")
        self.tables = ["Extract.Extract"]  # Default Tableau extract table

    # ============================================
    # Data Reading
    # ============================================

    def read_table(self, table_name: str, limit: int = 10000) -> pd.DataFrame:
        """
        Read table data into pandas DataFrame

        Args:
            table_name: Table name (schema.table format)
            limit: Maximum rows to read

        Returns:
            DataFrame with table data
        """
        if self.use_hyper_api:
            return self._read_table_hyper(table_name, limit)
        else:
            return self._read_table_duckdb(table_name, limit)

    def _read_table_hyper(self, table_name: str, limit: int) -> pd.DataFrame:
        """Read table using native Hyper API"""
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(hyper.endpoint, str(self.hyper_file)) as connection:
                # Parse schema.table
                parts = table_name.split(".")
                schema = parts[0] if len(parts) > 1 else "Extract"
                table = parts[-1]

                query = f'SELECT * FROM "{schema}"."{table}" LIMIT {limit}'

                with connection.execute_query(query) as result:
                    # Get column names
                    columns = [col.name for col in result.schema.columns]

                    # Fetch all rows
                    rows = []
                    for row in result:
                        rows.append(list(row))

                    df = pd.DataFrame(rows, columns=columns)
                    logger.info(f"Read {len(df)} rows from {table_name}")
                    return df

    def _read_table_duckdb(self, table_name: str, limit: int) -> pd.DataFrame:
        """Read table using DuckDB (fallback)"""
        # Placeholder - would need actual conversion logic
        logger.warning("DuckDB table reading not fully implemented. Returning empty DataFrame.")
        return pd.DataFrame()

    # ============================================
    # Data Profiling
    # ============================================

    def profile_table(self, table_name: str, sample_size: int = 10000) -> TableProfile:
        """
        Profile a table for data characteristics

        Args:
            table_name: Table to profile
            sample_size: Number of rows to sample

        Returns:
            TableProfile with statistics
        """
        logger.info(f"Profiling table: {table_name}")

        # Read sample data
        df = self.read_table(table_name, limit=sample_size)

        if df.empty:
            logger.warning(f"Table {table_name} is empty")
            return TableProfile(
                table_name=table_name,
                row_count=0,
                column_count=0,
                columns=[],
                sample_data=df,
                primary_key_candidates=[]
            )

        # Profile each column
        column_profiles = []
        for col in df.columns:
            profile = self._profile_column(df, col)
            column_profiles.append(profile)

        # Identify primary key candidates
        pk_candidates = self._identify_primary_keys(column_profiles, len(df))

        table_profile = TableProfile(
            table_name=table_name,
            row_count=len(df),
            column_count=len(df.columns),
            columns=column_profiles,
            sample_data=df.head(100),  # Keep only 100 rows for memory
            primary_key_candidates=pk_candidates
        )

        self.profiles[table_name] = table_profile
        return table_profile

    def _profile_column(self, df: pd.DataFrame, column_name: str) -> ColumnProfile:
        """Profile a single column"""
        col_data = df[column_name]

        # Basic stats
        row_count = len(col_data)
        null_count = col_data.isna().sum()
        null_percent = (null_count / row_count * 100) if row_count > 0 else 0
        distinct_count = col_data.nunique()
        cardinality = (distinct_count / row_count) if row_count > 0 else 0

        # Data type detection
        is_numeric = pd.api.types.is_numeric_dtype(col_data)
        data_type = str(col_data.dtype)

        # Sample values (exclude nulls)
        sample_values = col_data.dropna().head(10).tolist()

        # Numeric stats
        min_value = None
        max_value = None
        mean_value = None

        if is_numeric:
            min_value = float(col_data.min()) if not col_data.isna().all() else None
            max_value = float(col_data.max()) if not col_data.isna().all() else None
            mean_value = float(col_data.mean()) if not col_data.isna().all() else None

        return ColumnProfile(
            column_name=column_name,
            data_type=data_type,
            row_count=row_count,
            null_count=int(null_count),
            null_percent=round(null_percent, 2),
            distinct_count=int(distinct_count),
            cardinality=round(cardinality, 4),
            sample_values=sample_values,
            is_numeric=is_numeric,
            min_value=min_value,
            max_value=max_value,
            mean_value=mean_value
        )

    def _identify_primary_keys(self, columns: List[ColumnProfile], row_count: int) -> List[str]:
        """Identify potential primary key columns"""
        candidates = []

        for col in columns:
            # Primary key criteria:
            # 1. No nulls
            # 2. All unique values (cardinality = 1.0)
            # 3. Sufficient row count
            if col.null_count == 0 and col.cardinality == 1.0 and row_count > 1:
                candidates.append(col.column_name)

        return candidates

    # ============================================
    # Validation Test Slice Generation
    # ============================================

    def generate_test_slices(
        self,
        table_name: str,
        dimensions: List[str],
        max_slices: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Generate test slices for validation

        A "slice" is a specific combination of dimension values
        used to validate calculations.

        Example:
            dimensions = ["Region", "Year"]
            returns: [
                {"Region": "East", "Year": 2024},
                {"Region": "West", "Year": 2024},
                ...
            ]

        Args:
            table_name: Source table
            dimensions: Dimension columns
            max_slices: Maximum number of slices to generate

        Returns:
            List of dimension value combinations
        """
        df = self.read_table(table_name, limit=100000)

        if df.empty or not dimensions:
            return []

        # Verify dimensions exist
        missing = [d for d in dimensions if d not in df.columns]
        if missing:
            logger.warning(f"Dimensions not found in table: {missing}")
            return []

        # Get unique combinations
        unique_combinations = df[dimensions].drop_duplicates().head(max_slices)

        # Convert to list of dicts
        slices = unique_combinations.to_dict('records')

        logger.info(f"Generated {len(slices)} test slices for dimensions: {dimensions}")
        return slices

    # ============================================
    # Formula Execution (Ground Truth Extraction)
    # ============================================

    def execute_tableau_formula(
        self,
        table_name: str,
        formula: str,
        filters: Optional[Dict[str, Any]] = None
    ) -> Optional[float]:
        """
        Execute a Tableau-like formula against Hyper data

        This provides "ground truth" for validation.

        Args:
            table_name: Source table
            formula: Tableau formula (e.g., "SUM([Sales])")
            filters: Dimension filters (e.g., {"Region": "East"})

        Returns:
            Calculated value
        """
        logger.debug(f"Executing formula: {formula} with filters: {filters}")

        # Read data
        df = self.read_table(table_name, limit=1000000)

        if df.empty:
            return None

        # Apply filters
        if filters:
            for col, value in filters.items():
                if col in df.columns:
                    df = df[df[col] == value]

        # Parse and execute formula
        try:
            result = self._parse_and_execute_formula(df, formula)
            logger.debug(f"Formula result: {result}")
            return result
        except Exception as e:
            logger.error(f"Failed to execute formula: {e}")
            return None

    def _parse_and_execute_formula(self, df: pd.DataFrame, formula: str) -> Optional[float]:
        """
        Parse Tableau formula and execute against DataFrame

        Supports:
        - SUM([Column])
        - AVG([Column])
        - COUNT([Column])
        - COUNTD([Column])
        - Simple division: SUM([A]) / SUM([B])
        """
        import re

        # Clean formula
        formula = formula.strip()

        # Pattern: SUM([Column])
        sum_match = re.match(r'SUM\(\[(.+?)\]\)', formula, re.IGNORECASE)
        if sum_match:
            col = sum_match.group(1)
            return float(df[col].sum()) if col in df.columns else None

        # Pattern: AVG([Column])
        avg_match = re.match(r'AVG\(\[(.+?)\]\)', formula, re.IGNORECASE)
        if avg_match:
            col = avg_match.group(1)
            return float(df[col].mean()) if col in df.columns else None

        # Pattern: COUNT([Column])
        count_match = re.match(r'COUNT\(\[(.+?)\]\)', formula, re.IGNORECASE)
        if count_match:
            col = count_match.group(1)
            return float(df[col].count()) if col in df.columns else None

        # Pattern: COUNTD([Column])
        countd_match = re.match(r'COUNTD\(\[(.+?)\]\)', formula, re.IGNORECASE)
        if countd_match:
            col = countd_match.group(1)
            return float(df[col].nunique()) if col in df.columns else None

        # Pattern: SUM([A]) / SUM([B])
        ratio_match = re.match(
            r'SUM\(\[(.+?)\]\)\s*/\s*SUM\(\[(.+?)\]\)',
            formula,
            re.IGNORECASE
        )
        if ratio_match:
            col_a = ratio_match.group(1)
            col_b = ratio_match.group(2)

            if col_a in df.columns and col_b in df.columns:
                numerator = df[col_a].sum()
                denominator = df[col_b].sum()

                if denominator != 0:
                    return float(numerator / denominator)
                else:
                    return 0.0

        # If no pattern matched, raise error
        raise ValueError(f"Unsupported formula pattern: {formula}")

    # ============================================
    # Data Quality Checks
    # ============================================

    def detect_duplicates(
        self,
        table_name: str,
        sample_size: Optional[int] = None
    ) -> int:
        """
        Detect duplicate rows in a table

        PERFORMANCE: Uses sampling by default to avoid full table scans.
        For large tables (>100K rows), only samples first N rows.

        Args:
            table_name: Table to check
            sample_size: Max rows to sample (None = use smart default)

        Returns:
            Estimated duplicate count
        """
        # Smart sampling: Use 10K sample for large tables
        if sample_size is None:
            # First get row count efficiently
            row_count = self.get_row_count(table_name)

            if row_count > 100000:
                # Large table: sample 10K rows
                sample_size = 10000
                logger.info(f"Large table detected ({row_count} rows). Using sample size: {sample_size}")
            else:
                # Small table: check all rows
                sample_size = row_count

        # Read sample data
        df = self.read_table(table_name, limit=sample_size)

        if df.empty:
            return 0

        # Count duplicates in sample
        duplicate_count = len(df) - len(df.drop_duplicates())

        # If we sampled, extrapolate to full table
        if sample_size and sample_size < len(df):
            # Extrapolate based on sample rate
            duplicate_rate = duplicate_count / len(df)
            total_rows = self.get_row_count(table_name)
            estimated_duplicates = int(total_rows * duplicate_rate)

            logger.debug(
                f"Sampled {len(df)} rows, found {duplicate_count} duplicates "
                f"({duplicate_rate:.2%}). Estimated total: {estimated_duplicates}"
            )
            return estimated_duplicates
        else:
            return duplicate_count

    def get_row_count(self, table_name: str) -> int:
        """
        Get total row count for a table (efficient COUNT query)

        Args:
            table_name: Table name

        Returns:
            Number of rows
        """
        if self.use_hyper_api:
            return self._get_row_count_hyper(table_name)
        else:
            return self._get_row_count_duckdb(table_name)

    def _get_row_count_hyper(self, table_name: str) -> int:
        """Get row count using Hyper API"""
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(hyper.endpoint, str(self.hyper_file)) as connection:
                parts = table_name.split(".")
                schema = parts[0] if len(parts) > 1 else "Extract"
                table = parts[-1]

                query = f'SELECT COUNT(*) FROM "{schema}"."{table}"'

                with connection.execute_query(query) as result:
                    for row in result:
                        return int(row[0])
                return 0

    def _get_row_count_duckdb(self, table_name: str) -> int:
        """Get row count using DuckDB (fallback)"""
        logger.warning("DuckDB row count not implemented. Returning 0.")
        return 0

    def get_columns(self, table_name: str) -> List[Dict[str, str]]:
        """
        Get column metadata for a table

        Args:
            table_name: Table name

        Returns:
            List of column info dicts with 'name' and 'data_type'
        """
        if self.use_hyper_api:
            return self._get_columns_hyper(table_name)
        else:
            return self._get_columns_duckdb(table_name)

    def _get_columns_hyper(self, table_name: str) -> List[Dict[str, str]]:
        """Get columns using Hyper API"""
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(hyper.endpoint, str(self.hyper_file)) as connection:
                parts = table_name.split(".")
                schema = parts[0] if len(parts) > 1 else "Extract"
                table = parts[-1]

                # Query information schema
                query = f"""
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = '{schema}' AND table_name = '{table}'
                """

                columns = []
                with connection.execute_query(query) as result:
                    for row in result:
                        columns.append({
                            "name": row[0],
                            "data_type": row[1]
                        })
                return columns

    def _get_columns_duckdb(self, table_name: str) -> List[Dict[str, str]]:
        """Get columns using DuckDB (fallback)"""
        logger.warning("DuckDB column query not implemented. Returning empty list.")
        return []

    def get_column_uniqueness(self, table_name: str, column_name: str) -> float:
        """
        Calculate column uniqueness percentage (for primary key detection)

        Args:
            table_name: Table name
            column_name: Column name

        Returns:
            Uniqueness percentage (0-100)
        """
        if self.use_hyper_api:
            return self._get_column_uniqueness_hyper(table_name, column_name)
        else:
            return 0.0

    def _get_column_uniqueness_hyper(self, table_name: str, column_name: str) -> float:
        """Get column uniqueness using Hyper API"""
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(hyper.endpoint, str(self.hyper_file)) as connection:
                parts = table_name.split(".")
                schema = parts[0] if len(parts) > 1 else "Extract"
                table = parts[-1]

                # Get total rows and distinct values
                query = f"""
                    SELECT
                        COUNT(*) as total_rows,
                        COUNT(DISTINCT "{column_name}") as distinct_values
                    FROM "{schema}"."{table}"
                """

                with connection.execute_query(query) as result:
                    for row in result:
                        total = row[0]
                        distinct = row[1]

                        if total == 0:
                            return 0.0

                        uniqueness = (distinct / total) * 100
                        return round(uniqueness, 2)
                return 0.0

    # ============================================
    # Utility Methods
    # ============================================

    def get_table_summary(self, table_name: str) -> Dict[str, Any]:
        """Get summary statistics for a table"""
        if table_name not in self.profiles:
            self.profile_table(table_name)

        profile = self.profiles[table_name]

        return {
            "table_name": table_name,
            "row_count": profile.row_count,
            "column_count": profile.column_count,
            "columns": [
                {
                    "name": col.column_name,
                    "type": col.data_type,
                    "null_percent": col.null_percent,
                    "distinct_count": col.distinct_count,
                    "cardinality": col.cardinality
                }
                for col in profile.columns
            ],
            "primary_key_candidates": profile.primary_key_candidates
        }

    def export_sample_data(self, table_name: str, output_path: str, limit: int = 1000):
        """Export sample data to CSV for testing"""
        df = self.read_table(table_name, limit=limit)
        df.to_csv(output_path, index=False)
        logger.info(f"Exported {len(df)} rows to {output_path}")

    def list_tables(self) -> List[str]:
        """Get list of all tables in Hyper file"""
        return self.tables.copy()

    def close(self):
        """Clean up resources"""
        if hasattr(self, 'duckdb_conn'):
            self.duckdb_conn.close()
