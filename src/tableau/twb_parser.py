"""Tableau TWB/TWBX Parser - Extract metadata from Tableau workbooks"""
import zipfile
import tempfile
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass
from lxml import etree
from io import BytesIO
from loguru import logger
from enum import Enum


class VisualType(Enum):
    """Tableau visual types - critical for DAX context"""
    TEXT_TABLE = "text_table"  # Simple table with text
    MATRIX = "matrix"  # Crosstab/Pivot table
    BAR_CHART = "bar"
    LINE_CHART = "line"
    AREA_CHART = "area"
    SCATTER = "scatter"
    PIE_CHART = "pie"
    MAP = "map"
    CARD = "card"  # Single value display (most important!)
    GANTT = "gantt"
    HEATMAP = "heatmap"
    UNKNOWN = "unknown"


@dataclass
class CalculatedField:
    """Tableau calculated field"""
    name: str
    formula: str
    calc_type: str  # calculated-column, aggregation, table-calc
    datatype: str
    role: str  # measure, dimension
    caption: Optional[str] = None
    default_format: Optional[str] = None
    folder: Optional[str] = None


@dataclass
class LODExpression:
    """Level of Detail expression"""
    name: str
    lod_type: str  # FIXED, INCLUDE, EXCLUDE
    dimensions: List[str]
    aggregation: str
    formula: str


@dataclass
class TableauFilter:
    """Tableau filter"""
    field: str
    filter_type: str  # categorical, quantitative, context
    values: List[str]
    is_context_filter: bool
    worksheet: Optional[str] = None
    operator: Optional[str] = None


@dataclass
class TableauParameter:
    """Tableau parameter"""
    name: str
    datatype: str
    current_value: Any
    allowable_values: List[Any]
    alias: Optional[str] = None


@dataclass
class Worksheet:
    """Tableau worksheet with visual context"""
    name: str
    visual_type: VisualType  # NEW: What kind of visual is this?
    rows_fields: List[str]
    columns_fields: List[str]
    marks_fields: List[str]
    filters: List[str]
    mark_type: str = "automatic"  # bar, line, text, etc.


@dataclass
class Dashboard:
    """Tableau dashboard"""
    name: str
    worksheets: List[str]


@dataclass
class DataSource:
    """Tableau data source"""
    name: str
    connection_type: str  # hyper, excel, sqlserver, etc.
    tables: List[str]
    relationships: List[Dict[str, Any]]


class TableauTWBParser:
    """
    Production-grade Tableau TWB/TWBX parser

    Handles:
    - Calculated fields (all types)
    - LOD expressions
    - Parameters
    - Filters (including context filters - CRITICAL for DAX conversion)
    - Table calculations
    - Worksheets and dashboards
    - Data sources and connections
    """

    def __init__(self, file_path: str):
        """
        Initialize parser with a TWB or TWBX file

        Args:
            file_path: Path to .twb or .twbx file
        """
        self.file_path = Path(file_path)
        self.twb_xml: Optional[bytes] = None
        self.tree: Optional[etree._Element] = None
        self.hyper_files: List[Path] = []
        self.csv_files: List[Path] = []

        self._extract_and_parse()

    def _extract_and_parse(self):
        """Extract TWB from TWBX if needed and parse XML"""
        if self.file_path.suffix.lower() == ".twbx":
            self._extract_from_twbx()
        elif self.file_path.suffix.lower() == ".twb":
            with open(self.file_path, 'rb') as f:
                self.twb_xml = f.read()
        else:
            raise ValueError(f"Unsupported file type: {self.file_path.suffix}")

        # Parse XML
        try:
            self.tree = etree.fromstring(self.twb_xml)
            logger.info(f"Parsed Tableau workbook: {self.file_path.name}")
        except Exception as e:
            raise ValueError(f"Failed to parse TWB XML: {e}")

    def _extract_from_twbx(self):
        """Extract TWB XML and data files from TWBX (ZIP) file"""
        try:
            with zipfile.ZipFile(self.file_path, 'r') as zf:
                # Find TWB file
                twb_files = [f for f in zf.namelist() if f.endswith('.twb')]
                if not twb_files:
                    raise ValueError("No TWB file found in TWBX archive")

                # Read TWB XML
                self.twb_xml = zf.read(twb_files[0])

                # Extract hyper files to temp directory for later use
                temp_dir = Path(tempfile.gettempdir()) / "tableau_extracts" / self.file_path.stem
                temp_dir.mkdir(parents=True, exist_ok=True)

                for file in zf.namelist():
                    if file.endswith('.hyper'):
                        extracted_path = temp_dir / Path(file).name
                        with open(extracted_path, 'wb') as f:
                            f.write(zf.read(file))
                        self.hyper_files.append(extracted_path)
                        logger.debug(f"Extracted Hyper file: {extracted_path}")

                    elif file.endswith(('.csv', '.xlsx', '.xls')):
                        extracted_path = temp_dir / Path(file).name
                        with open(extracted_path, 'wb') as f:
                            f.write(zf.read(file))
                        self.csv_files.append(extracted_path)
                        logger.debug(f"Extracted data file: {extracted_path}")

                logger.info(f"Extracted {len(self.hyper_files)} Hyper files and {len(self.csv_files)} CSV/Excel files")

        except Exception as e:
            raise ValueError(f"Failed to extract TWBX file: {e}")

    # ============================================
    # Calculated Fields Parsing
    # ============================================

    def parse_calculated_fields(self) -> List[CalculatedField]:
        """
        Parse all calculated fields from datasources

        XML structure:
        <datasources>
          <datasource>
            <column caption='Profit Ratio' datatype='real' name='[Calculation_123]'
                    role='measure' type='quantitative'>
              <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])'/>
            </column>
          </datasource>
        </datasources>
        """
        calculations = []

        # XPath: all columns with <calculation> child
        for col in self.tree.xpath("//column[calculation]"):
            calc_elem = col.find("calculation")

            if calc_elem is None:
                continue

            # Extract attributes
            name = col.get("name", "").strip("[]")
            formula = calc_elem.get("formula", "")

            if not formula:
                continue

            calc = CalculatedField(
                name=name,
                formula=formula,
                calc_type=calc_elem.get("class", "tableau"),
                datatype=col.get("datatype", ""),
                role=col.get("role", ""),
                caption=col.get("caption"),
                default_format=col.get("default-format"),
                folder=col.get("folder")
            )

            calculations.append(calc)

        logger.info(f"Parsed {len(calculations)} calculated fields")
        return calculations

    def parse_lod_expressions(self) -> List[LODExpression]:
        """
        Extract Level of Detail expressions

        LOD formulas in Tableau:
        - FIXED: {FIXED [Region]: SUM([Sales])}
        - INCLUDE: {INCLUDE [Category]: AVG([Profit])}
        - EXCLUDE: {EXCLUDE [Month]: SUM([Revenue])}
        """
        lod_expressions = []
        calculations = self.parse_calculated_fields()

        # Regex to detect LOD syntax
        lod_pattern = re.compile(
            r'\{(FIXED|INCLUDE|EXCLUDE)\s+([^\}:]+):\s*(.+)\}',
            re.IGNORECASE
        )

        for calc in calculations:
            match = lod_pattern.search(calc.formula)

            if match:
                lod_type = match.group(1).upper()
                dimensions_str = match.group(2)
                aggregation_formula = match.group(3)

                # Parse dimensions
                dimensions = [
                    d.strip().strip("[]")
                    for d in dimensions_str.split(",")
                ]

                lod = LODExpression(
                    name=calc.name,
                    lod_type=lod_type,
                    dimensions=dimensions,
                    aggregation=aggregation_formula,
                    formula=calc.formula
                )

                lod_expressions.append(lod)

        logger.info(f"Parsed {len(lod_expressions)} LOD expressions")
        return lod_expressions

    # ============================================
    # Filters Parsing (CRITICAL for DAX)
    # ============================================

    def parse_filters(self) -> List[TableauFilter]:
        """
        Parse all filters, identifying context filters (CRITICAL)

        Context filters in Tableau create a "context" that affects subsequent filters.
        In DAX, this maps to specific CALCULATE patterns.

        XML structure:
        <worksheet name='Sheet1'>
          <filter class='categorical' column='[Region]' filter-group='1'>
            <groupfilter function='member' level='[Region]' member='&quot;East&quot;'/>
          </filter>
          <filter class='context' column='[Year]' ...>
        </worksheet>
        """
        filters = []

        for ws in self.tree.xpath("//worksheet"):
            ws_name = ws.get("name")

            for filter_elem in ws.xpath(".//filter"):
                field = filter_elem.get("column", "").strip("[]")
                filter_class = filter_elem.get("class", "")

                # Detect context filter
                is_context = (filter_class == "context")

                # Extract filter values
                values = []
                for group_filter in filter_elem.xpath(".//groupfilter"):
                    member = group_filter.get("member", "")
                    # Clean up Tableau's quoted format
                    member = member.strip('"&quot;')
                    if member:
                        values.append(member)

                # Extract range filter values
                min_val = filter_elem.get("min")
                max_val = filter_elem.get("max")
                if min_val or max_val:
                    values.extend([v for v in [min_val, max_val] if v])

                filter_obj = TableauFilter(
                    field=field,
                    filter_type=filter_class,
                    values=values,
                    is_context_filter=is_context,
                    worksheet=ws_name
                )

                filters.append(filter_obj)

        context_filters_count = sum(1 for f in filters if f.is_context_filter)
        logger.info(f"Parsed {len(filters)} filters ({context_filters_count} context filters)")
        return filters

    # ============================================
    # Parameters Parsing
    # ============================================

    def parse_parameters(self) -> List[TableauParameter]:
        """
        Parse Tableau parameters

        XML structure:
        <column caption='Date Granularity' datatype='string'
                name='[Parameter 1]' param-domain-type='list'
                role='measure' type='nominal' value='&quot;Month&quot;'>
          <calculation class='tableau' formula='&quot;Month&quot;'/>
          <members>
            <member alias='Year' value='&quot;Year&quot;'/>
            <member alias='Quarter' value='&quot;Quarter&quot;'/>
            <member alias='Month' value='&quot;Month&quot;'/>
          </members>
        </column>
        """
        parameters = []

        for col in self.tree.xpath("//column[@param-domain-type]"):
            name = col.get("name", "").strip("[]")
            datatype = col.get("datatype", "")
            current_value = col.get("value", "").strip('&quot;"')

            # Extract allowable values from <members>
            allowable_values = []
            for member in col.xpath(".//member"):
                value = member.get("value", "").strip('&quot;"')
                if value:
                    allowable_values.append(value)

            param = TableauParameter(
                name=name,
                datatype=datatype,
                current_value=current_value,
                allowable_values=allowable_values,
                alias=col.get("caption")
            )

            parameters.append(param)

        logger.info(f"Parsed {len(parameters)} parameters")
        return parameters

    # ============================================
    # Worksheets & Dashboards Parsing
    # ============================================

    def parse_worksheets(self) -> List[Worksheet]:
        """
        Extract worksheet metadata (what fields are used where)

        This provides visual context for DAX generation.
        """
        worksheets = []

        for ws in self.tree.xpath("//worksheet"):
            name = ws.get("name")

            # Extract fields from rows shelf
            rows_fields = self._extract_shelf_fields(ws, ".//rows//field")

            # Extract fields from columns shelf
            columns_fields = self._extract_shelf_fields(ws, ".//columns//field")

            # Extract fields from marks (color, size, label, etc.)
            marks_fields = []
            for encoding in ws.xpath(".//encoding"):
                for field_elem in encoding.xpath(".//field"):
                    field_name = field_elem.get("name", "").strip("[]")
                    if field_name and field_name not in marks_fields:
                        marks_fields.append(field_name)

            # Extract filter fields
            filters = [
                f.get("column", "").strip("[]")
                for f in ws.xpath(".//filter")
            ]

            # NEW: Detect visual type
            visual_type, mark_type = self._detect_visual_type(ws, rows_fields, columns_fields, marks_fields)

            worksheet = Worksheet(
                name=name,
                visual_type=visual_type,  # NEW
                rows_fields=rows_fields,
                columns_fields=columns_fields,
                marks_fields=marks_fields,
                filters=filters,
                mark_type=mark_type  # NEW
            )

            worksheets.append(worksheet)

        logger.info(f"Parsed {len(worksheets)} worksheets")
        return worksheets

    def _extract_shelf_fields(self, ws_elem, xpath: str) -> List[str]:
        """Extract fields from shelves (rows, columns, marks)"""
        fields = []

        for field_elem in ws_elem.xpath(xpath):
            field_name = field_elem.get("name", "").strip("[]")
            if field_name and field_name not in fields:
                fields.append(field_name)

        return fields

    def _detect_visual_type(
        self,
        ws_elem,
        rows_fields: List[str],
        columns_fields: List[str],
        marks_fields: List[str]
    ) -> tuple[VisualType, str]:
        """
        Detect visual type from worksheet XML structure

        This is CRITICAL for DAX generation because:
        - A Card visual (single value) needs a scalar DAX measure
        - A Matrix needs DAX with proper grouping context
        - A Bar chart may need sorted dimensions

        Logic:
        1. Check mark type (bar, line, text, etc.)
        2. Check shelf structure (rows + columns = matrix)
        3. Check if no dimensions = card

        Returns:
            (VisualType, mark_type_string)
        """
        # Step 1: Get mark type from style
        mark_type = "automatic"
        style_rules = ws_elem.xpath(".//style/style-rule[@element='mark']")

        if style_rules:
            for rule in style_rules:
                # Try to get mark class attribute
                mark_class = rule.xpath(".//format[@attr='mark-type']/@value")
                if mark_class:
                    mark_type = mark_class[0]
                    break

                # Alternative: check format value
                format_vals = rule.xpath(".//format[@attr='mark-type']/@value | .//format[@value]")
                if format_vals:
                    mark_type = format_vals[0]
                    break

        # Step 2: Detect visual type based on structure
        has_rows = len(rows_fields) > 0
        has_cols = len(columns_fields) > 0
        has_measures = len(marks_fields) > 0

        # Pattern 1: No dimensions at all = CARD (single value)
        if not has_rows and not has_cols:
            return (VisualType.CARD, mark_type)

        # Pattern 2: Both rows and columns = MATRIX/CROSSTAB
        if has_rows and has_cols:
            if mark_type in ["text", "square"]:
                return (VisualType.MATRIX, mark_type)
            elif mark_type == "square":
                return (VisualType.HEATMAP, mark_type)
            else:
                # Might be a chart with dual axis
                return (VisualType.UNKNOWN, mark_type)

        # Pattern 3: Only rows or only columns
        if has_rows or has_cols:
            if mark_type == "bar":
                return (VisualType.BAR_CHART, mark_type)
            elif mark_type == "line":
                return (VisualType.LINE_CHART, mark_type)
            elif mark_type in ["area", "polygon"]:
                return (VisualType.AREA_CHART, mark_type)
            elif mark_type == "circle":
                return (VisualType.SCATTER, mark_type)
            elif mark_type == "pie":
                return (VisualType.PIE_CHART, mark_type)
            elif mark_type == "text":
                return (VisualType.TEXT_TABLE, mark_type)
            elif mark_type == "map":
                return (VisualType.MAP, mark_type)
            elif mark_type == "gantt":
                return (VisualType.GANTT, mark_type)

        # Default: Unknown
        return (VisualType.UNKNOWN, mark_type)

    def parse_dashboards(self) -> List[Dashboard]:
        """Parse dashboard metadata"""
        dashboards = []

        for db in self.tree.xpath("//dashboard"):
            name = db.get("name")

            # Extract worksheet references
            worksheets = []
            for zone in db.xpath(".//zone[@type='worksheet']"):
                ws_name = zone.get("name")
                if ws_name:
                    worksheets.append(ws_name)

            dashboard = Dashboard(
                name=name,
                worksheets=worksheets
            )

            dashboards.append(dashboard)

        logger.info(f"Parsed {len(dashboards)} dashboards")
        return dashboards

    # ============================================
    # Data Sources Parsing
    # ============================================

    def parse_data_sources(self) -> List[DataSource]:
        """
        Extract data source connections

        Returns info about:
        - Connection type (extract, live, hyper)
        - Database name
        - Tables
        - Joins
        """
        data_sources = []

        for ds in self.tree.xpath("//datasource[@name]"):
            ds_name = ds.get("name")

            if ds_name == "Parameters":  # Skip parameters datasource
                continue

            # Extract connection info
            connections = ds.xpath(".//connection")
            connection_type = "unknown"
            tables = []
            relationships = []

            if connections:
                conn = connections[0]
                connection_type = conn.get("class", "unknown")

                # Extract tables
                for relation in conn.xpath(".//relation"):
                    table_name = relation.get("name") or relation.get("table")
                    if table_name:
                        tables.append(table_name)

                # Extract relationships (joins)
                for clause in conn.xpath(".//clause[@type='join']"):
                    relationship = {
                        "type": clause.get("join", "inner"),
                        "expression": clause.get("expression", "")
                    }
                    relationships.append(relationship)

            data_source = DataSource(
                name=ds_name,
                connection_type=connection_type,
                tables=tables,
                relationships=relationships
            )

            data_sources.append(data_source)

        logger.info(f"Parsed {len(data_sources)} data sources")
        return data_sources

    # ============================================
    # Utility Methods
    # ============================================

    def get_used_calculations(self) -> Dict[str, List[str]]:
        """
        Map calculations to worksheets where they're used

        Returns:
            Dict mapping calc_name -> [worksheet_names]
        """
        usage_map = {}

        worksheets = self.parse_worksheets()
        calculations = self.parse_calculated_fields()

        calc_names = {calc.name for calc in calculations}

        for ws in worksheets:
            all_fields = ws.rows_fields + ws.columns_fields + ws.marks_fields

            for field in all_fields:
                if field in calc_names:
                    if field not in usage_map:
                        usage_map[field] = []
                    usage_map[field].append(ws.name)

        return usage_map

    def get_visual_context(self, calc_name: str) -> Dict[str, Any]:
        """
        Get visual context for a specific calculation

        Args:
            calc_name: Name of the calculation

        Returns:
            Dictionary with visual context information
        """
        worksheets = self.parse_worksheets()
        context = {
            "used_in": [],
            "visual_types": set(),
            "partition_by": set(),
            "filters": set()
        }

        for ws in worksheets:
            all_fields = ws.rows_fields + ws.columns_fields + ws.marks_fields

            if calc_name in all_fields:
                context["used_in"].append(ws.name)

                # Determine partition context (grouping dimensions)
                dimensions = [
                    f for f in ws.rows_fields + ws.columns_fields
                    if f != calc_name
                ]
                context["partition_by"].update(dimensions)

                # Add filters
                context["filters"].update(ws.filters)

        # Convert sets to lists for JSON serialization
        context["partition_by"] = list(context["partition_by"])
        context["filters"] = list(context["filters"])

        return context

    def cleanup_temp_files(self):
        """Clean up extracted temporary files"""
        for file_path in self.hyper_files + self.csv_files:
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete temp file {file_path}: {e}")
