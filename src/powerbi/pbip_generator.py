"""
PBIP Generator — dynamically generates a complete Power BI Project folder structure.
No static templates or files required.
"""
import json
import re
import uuid
from pathlib import Path
from typing import Dict, List, Any
import pandas as pd
from loguru import logger
from src.powerbi.pbix_injector import Relationship

class PBIPGenerator:
    """Generate a complete Power BI Project (PBIP) folder structure programmatically."""

    DATA_TYPE_MAP = {
        "VARCHAR": "string", "CHAR": "string", "TEXT": "string", "STRING": "string",
        "INT32": "int64", "INT64": "int64", "INTEGER": "int64", "INT": "int64",
        "DOUBLE": "double", "FLOAT": "double", "DECIMAL": "double", "REAL": "double",
        "BOOL": "boolean", "BOOLEAN": "boolean",
        "DATE": "dateTime", "DATETIME": "dateTime", "TIMESTAMP": "dateTime", "TIME": "dateTime",
        "string": "string", "real": "double", "": "string",
    }

    def __init__(self, project_name: str, output_dir: Path):
        # We clean the project name to make it safe for file systems
        self.project_name = re.sub(r"[^\w\-]", "_", project_name)
        self.output_dir = output_dir
        
        self.semantic_model_dir = self.output_dir / f"{self.project_name}.SemanticModel"
        self.semantic_definition_dir = self.semantic_model_dir / "definition"
        self.tables_dir = self.semantic_definition_dir / "tables"
        
        self.report_dir = self.output_dir / f"{self.project_name}.Report"
        self.report_definition_dir = self.report_dir / "definition"

    def generate(
        self,
        tables: Dict[str, pd.DataFrame],
        relationships: List[Relationship],
        measures: List[Dict[str, str]]
    ) -> Path:
        """Generate the complete PBIP project folder structure from scratch."""
        # Create all necessary directories
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        self.report_definition_dir.mkdir(parents=True, exist_ok=True)

        # 1. Project root files
        self._write_pbip_file()

        # 2. Semantic Model files
        self._write_pbism_file()
        self._write_platform_file(self.semantic_model_dir, "SemanticModel")
        self._write_database_tmdl()
        self._write_model_tmdl(tables)
        self._write_table_tmdls(tables)
        self._write_measures_tmdl(measures)
        self._write_relationships_tmdl(relationships)

        # 3. Report files
        self._write_pbir_file()
        self._write_platform_file(self.report_dir, "Report")
        self._write_report_json()

        logger.info(f"Successfully generated dynamic PBIP project at: {self.output_dir}")
        return self.output_dir

    def _write_pbip_file(self):
        pbip = {
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{self.project_name}.Report"}}],
            "settings": {"enableAutoRecovery": True},
        }
        (self.output_dir / f"{self.project_name}.pbip").write_text(
            json.dumps(pbip, indent=2), encoding="utf-8"
        )

    def _write_pbism_file(self):
        pbism = {
            "version": "4.2",
            "settings": {}
        }
        (self.semantic_model_dir / "definition.pbism").write_text(
            json.dumps(pbism, indent=2), encoding="utf-8"
        )

    def _write_platform_file(self, target_dir: Path, item_type: str):
        platform = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {
                "type": item_type,
                "displayName": self.project_name
            },
            "config": {
                "version": "2.0",
                "logicalId": str(uuid.uuid4())
            }
        }
        (target_dir / ".platform").write_text(
            json.dumps(platform, indent=2), encoding="utf-8"
        )

    def _write_database_tmdl(self):
        content = "database\n\tcompatibilityLevel: 1600\n"
        (self.semantic_definition_dir / "database.tmdl").write_text(content, encoding="utf-8")

    def _write_model_tmdl(self, tables: Dict[str, pd.DataFrame]):
        # Model TMDL header
        lines = [
            "model Model",
            "\tculture: en-US",
            "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
            "\tsourceQueryCulture: en-US",
            "\tdataAccessOptions",
            "\t\tlegacyRedirects",
            "\t\treturnErrorValuesAsNull",
            "",
            "annotation __PBI_TimeIntelligenceEnabled = 1",
            "",
            'annotation PBI_ProTooling = ["DevMode"]',
            ""
        ]
        
        # Add ref table lines
        for table_name in tables.keys():
            clean_name = self._clean_column_name(table_name)
            if clean_name:
                tmdl_token = self._make_tmdl_table_name(clean_name)
                lines.append(f"ref table {tmdl_token}")
                
        # Also reference MeasuresTable if there are measures to write
        lines.append("ref table MeasuresTable")
        
        (self.semantic_definition_dir / "model.tmdl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _clean_column_name(self, name: str) -> str:
        name = str(name)
        name = re.sub(r'["\'`]', '', name)
        return name.strip()

    def _make_tmdl_table_name(self, name: str) -> str:
        if re.search(r"[\s\-\.\(\)'!#]", name):
            safe = name.replace("'", "''")
            return f"'{safe}'"
        return name

    def _get_tmdl_datatype(self, dtype) -> str:
        if pd.api.types.is_float_dtype(dtype):
            return "double"
        if pd.api.types.is_integer_dtype(dtype):
            return "int64"
        if pd.api.types.is_datetime64_any_dtype(dtype):
            return "dateTime"
        if pd.api.types.is_bool_dtype(dtype):
            return "boolean"
        return "string"

    def _format_cell_value(self, val) -> str:
        if pd.isna(val) or val is None:
            return "BLANK()"
        if isinstance(val, float):
            if val == int(val):
                return str(int(val))
            return str(val)
        if isinstance(val, bool):
            return "TRUE" if val else "FALSE"
        if isinstance(val, int):
            return str(val)
        safe = str(val).replace('"', '""')
        return f'"{safe}"'

    def _build_datatable_dax(self, df: pd.DataFrame) -> str:
        headers = []
        for col in df.columns:
            tmdl_type = self._get_tmdl_datatype(df[col].dtype)
            dax_type_map = {
                "int64": "INTEGER",
                "double": "DOUBLE",
                "dateTime": "DATETIME",
                "boolean": "BOOLEAN",
                "string": "STRING",
            }
            dax_type = dax_type_map.get(tmdl_type, "STRING")
            headers.append(f'"{col}", {dax_type}')

        header_str = ",\n\t\t\t\t".join(headers)

        row_strings = []
        for _, row in df.iterrows():
            vals = [self._format_cell_value(v) for v in row]
            row_strings.append(f"\t\t\t\t{{ {', '.join(vals)} }}")

        if row_strings:
            rows_str = ",\n".join(row_strings)
            body = f"{{\n{rows_str}\n\t\t\t\t}}"
        else:
            body = "{{ }}"

        return (
            f"\t\t\tDATATABLE (\n"
            f"\t\t\t\t{header_str},\n"
            f"\t\t\t\t{body}\n"
            f"\t\t\t)"
        )

    def _write_table_tmdls(self, tables: Dict[str, pd.DataFrame]):
        for table_name, df in tables.items():
            clean_name = self._clean_column_name(table_name)
            if not clean_name:
                continue

            tmdl_token = self._make_tmdl_table_name(clean_name)
            
            lines = [
                f"table {tmdl_token}",
                f"\tlineageTag: {uuid.uuid4()}",
                ""
            ]

            # Columns
            if df is not None and not df.empty:
                df = df.copy()
                df.columns = [self._clean_column_name(c) for c in df.columns]
                
                for col in df.columns:
                    dtype = self._get_tmdl_datatype(df[col].dtype)
                    lines.append(f"\tcolumn '{col}'")
                    lines.append(f"\t\tdataType: {dtype}")
                    lines.append(f"\t\tlineageTag: {uuid.uuid4()}")
                    lines.append(f"\t\tsummarizeBy: {'none' if dtype == 'string' else 'sum'}")
                    lines.append(f"\t\tsourceColumn: [{col}]")
                    lines.append("")

            # Partition
            lines.append(f"\tpartition {tmdl_token} = calculated")
            lines.append("\t\tmode: import")
            lines.append("\t\texpression =")
            
            if df is not None and not df.empty:
                try:
                    datatable_dax = self._build_datatable_dax(df)
                    lines.append(datatable_dax)
                except Exception as e:
                    logger.error(f"Failed to build DATATABLE for '{table_name}': {e}")
                    lines.append('\t\t\tDATATABLE ( "_dummy", STRING, { { "" } } )')
            else:
                lines.append('\t\t\tDATATABLE ( "_dummy", STRING, { { "" } } )')
                
            lines.append("")
            
            safe_filename = re.sub(r'[<>:"/\\|?*]', '_', clean_name)
            tmdl_path = self.tables_dir / f"{safe_filename}.tmdl"
            tmdl_path.write_text("\n".join(lines), encoding="utf-8")

    def _indent_dax(self, dax: str) -> str:
        dax = dax.strip()
        if "\n" not in dax:
            return dax
        lines = dax.splitlines()
        result = [lines[0]]
        for line in lines[1:]:
            result.append("\t\t\t" + line.lstrip())
        return "\n".join(result)

    def _write_measures_tmdl(self, measures: List[Dict[str, str]]):
        lines = [
            "table MeasuresTable",
            f"\tlineageTag: {uuid.uuid4()}",
            ""
        ]

        # Process and write measures
        for m in measures:
            m_name = m.get("name", "").strip()
            m_dax = m.get("dax", "0").strip()
            m_format = m.get("formatString", "0")

            if not m_name or not m_dax:
                continue

            indented_dax = self._indent_dax(m_dax)
            if "\n" in indented_dax:
                lines.append(f"\tmeasure '{m_name}' =")
                lines.append(f"\t\t\t{indented_dax}")
            else:
                lines.append(f"\tmeasure '{m_name}' = {indented_dax}")

            lines.append(f"\t\tformatString: {m_format}")
            lines.append(f"\t\tlineageTag: {uuid.uuid4()}")
            lines.append("")

        # Add calculated partition
        lines.append("\tpartition MeasuresTable = calculated")
        lines.append("\t\tmode: import")
        lines.append('\t\texpression =')
        lines.append('\t\t\tDATATABLE ( "Dummy", STRING, { { "1" } } )')
        lines.append("")

        mt_path = self.tables_dir / "MeasuresTable.tmdl"
        mt_path.write_text("\n".join(lines), encoding="utf-8")

    def _write_relationships_tmdl(self, relationships: List[Relationship]):
        lines = []
        for i, rel in enumerate(relationships):
            # Map cardinality
            card = rel.cardinality
            from_table, to_table = rel.from_table, rel.to_table
            from_col, to_col = rel.from_column, rel.to_column

            if card == "OneToMany":
                from_card, to_card = "many", "one"
                from_table, to_table = to_table, from_table
                from_col, to_col = to_col, from_col
            elif card == "ManyToOne":
                from_card, to_card = "many", "one"
            elif card == "OneToOne":
                from_card, to_card = "one", "one"
            elif card == "ManyToMany":
                from_card, to_card = "many", "many"
            else:
                from_card, to_card = "many", "one"

            # Map filter direction
            if rel.cross_filter_direction == "BothDirections":
                cross_filter = "bothDirections"
            else:
                cross_filter = "oneDirection"

            rel_name = f"rel_{from_table}_{from_col}_{to_table}_{to_col}"
            rel_name = re.sub(r"[^\w]", "_", rel_name)

            if lines:
                lines.append("")

            lines.append(f"relationship {rel_name}")
            lines.append(f"\tfromColumn: '{from_table}'.'{from_col}'")
            lines.append(f"\tfromCardinality: {from_card}")
            lines.append(f"\ttoColumn: '{to_table}'.'{to_col}'")
            lines.append(f"\ttoCardinality: {to_card}")
            lines.append(f"\tcrossFilteringBehavior: {cross_filter}")
            lines.append(f"\tisActive: {'true' if rel.is_active else 'false'}")

        relationships_path = self.semantic_definition_dir / "relationships.tmdl"
        relationships_path.write_text("\n".join(lines), encoding="utf-8")

    def _write_pbir_file(self):
        pbir = {
            "version": "1.0",
            "datasetReference": {
                "byPath": {
                    "path": f"../{self.project_name}.SemanticModel"
                }
            }
        }
        (self.report_dir / "definition.pbir").write_text(
            json.dumps(pbir, indent=2), encoding="utf-8"
        )

    def _write_report_json(self):
        report = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/1.0.0/schema.json",
            "themeCollection": {
                "baseTheme": {
                    "name": "CY24SU06",
                    "reportVersionAtImport": "5.55",
                    "type": "SharedResources"
                }
            },
            "pages": [
                {
                    "name": "Page 1",
                    "displayName": "Page 1",
                    "visuals": []
                }
            ]
        }
        (self.report_definition_dir / "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
