"""Tableau TWB/TWBX Parser - Extract metadata from Tableau workbooks"""
import zipfile
import tempfile
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field
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
    dimensions: List[str] = field(default_factory=list)
    measures: List[str] = field(default_factory=list)
    axes: Dict[str, Any] = field(default_factory=dict)


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
            # Extract fields from marks (color, size, label, etc.)
            marks_fields = []
            for encoding in ws.xpath(".//encoding"):
                # Case 1: Column as attribute (e.g. <encoding attr='color' column='[calc]' />)
                col_attr = encoding.get("column")
                if col_attr:
                    # Clean up: [sum:Calculation_...] -> Calculation_...
                    cleaned = col_attr.split(":")[-1].strip("[]")
                    # Or simple strip if no colons
                    if ":" not in col_attr:
                        cleaned = col_attr.strip("[]")
                    
                    if cleaned and cleaned not in marks_fields:
                        marks_fields.append(cleaned)

                # Case 2: Field as child element
                for field_elem in encoding.xpath(".//field"):
                    field_name = field_elem.get("name", "").strip("[]")
                    if field_name and field_name not in marks_fields:
                        marks_fields.append(field_name)

            # Extract filter fields
            filters = [
                f.get("column", "").strip("[]")
                for f in ws.xpath(".//filter")
            ]

            # NEW: Detect basic chart type from XML (Mark class)
            # This replaces the complex _detect_visual_type logic with the simpler reference logic
            basic_chart_type = self._detect_basic_chart_type(ws)
            
            # NEW: Extract detailed fields and implicit axes
            dimensions, measures = self._extract_detailed_fields(ws)
            
            # Use user's logic to refine visual type
            visual_type_str = self._infer_visual_type(basic_chart_type, dimensions, measures)
            
            # Map string back to Enum if possible, or use as value
            # The Worksheet dataclass expects VisualType enum, but we might need to be flexible or map it.
            # For now, let's try to map known ones, or default to UNKNOWN and store the string in mark_type or a new field.
            # actually, VisualType is an Enum. Let's see if we can map common ones.
            try:
                visual_type = VisualType(visual_type_str.lower())
            except ValueError:
                # If not in Enum (e.g. "Automatic", "Table"), map to closest or UNKNOWN
                if visual_type_str == "Table":
                    visual_type = VisualType.TEXT_TABLE
                elif visual_type_str == "Map":
                    visual_type = VisualType.MAP
                else:
                    visual_type = VisualType.UNKNOWN

            # Re-extract instance map for axes inference
            instance_map = self._extract_column_instance_map(ws)
            axes = self._infer_axes(visual_type_str, dimensions, measures, instance_map)

            worksheet = Worksheet(
                name=name,
                visual_type=visual_type, 
                rows_fields=rows_fields,
                columns_fields=columns_fields,
                marks_fields=marks_fields,
                filters=filters,
                mark_type=visual_type_str, # Store the inferred string here for API
                dimensions=dimensions,
                measures=measures,
                axes=axes
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
    # Detailed Metadata Extraction (New)
    # ============================================

    def _extract_column_instance_map(self, ws_elem) -> Dict[str, Dict[str, Any]]:
        """Map column instances to base columns and roles"""
        mapping = {}
        
        # Look for datasource-dependencies recursively
        # Often under table/view/datasource-dependencies
        for deps in ws_elem.xpath(".//datasource-dependencies"):
            base_roles = {}
            for col in deps.xpath("./column"):
                base_name = col.get("name", "").strip("[]")
                role = col.get("role")
                base_roles[base_name] = role
                
            for ci in deps.xpath("./column-instance"):
                instance = ci.get("name", "").strip("[]")
                base_col = ci.get("column", "").strip("[]")
                derivation = ci.get("derivation")
                role = base_roles.get(base_col)
                
                mapping[instance] = {
                    "base_column": base_col,
                    "role": role,
                    "derivation": derivation
                }
                
        return mapping

    def _detect_basic_chart_type(self, ws_elem):
        """Detect basic chart type from mark class (Reference Logic)"""
        # mark = ws.find("./pane/mark") -> in lxml xpath this is ./table/view/pane/mark or similar?
        # The reference code uses `ws.find("./pane/mark")`. 
        # In TWB XML, <worksheet> has <table> -> <view> -> <pane> -> <mark> usually?
        # Or <worksheet> -> <table> -> <pane> ?
        # Let's check typical structure. Usually it is worksheet/table/view OR worksheet/table/panes/pane
        # The reference code implies direct child. Let's try to match reference logic using .// which is safer
        
        marks = ws_elem.xpath(".//pane/mark")
        if marks:
            return marks[0].get("class", "Unknown")
        return "Automatic" # Ref says "Unknown" but fallback in infer_visual_type is "Automatic"

    def _extract_detailed_fields(self, ws_elem):
        """Extract dimensions and measures (Reference Logic)"""
        dimensions = set()
        measures = set()
        
        instance_map = self._extract_column_instance_map(ws_elem)
        
        # 1. Pane encodings
        for enc in ws_elem.xpath(".//pane/encodings/*"):
            col_ref = enc.get("column")
            if not col_ref:
                continue
                
            clean = col_ref.split(".")[-1].strip("[]")
            
            if clean in instance_map:
                meta = instance_map[clean]
                if meta["role"] == "dimension":
                    dimensions.add(meta["base_column"])
                elif meta["role"] == "measure":
                    measures.add(meta["base_column"])
            else:
                 # Fallback for when instance map miss
                 if ":" in clean:
                     parts = clean.split(":")
                     if len(parts) >= 2:
                         potential_name = parts[1]
                         measures.add(potential_name)

        # 2. Rows shelf
        for field in ws_elem.xpath(".//rows//field"):
            fname = field.get("name") or field.get("field")
            if fname:
                dimensions.add(fname.strip("[]"))
                
        # 3. Columns shelf
        for field in ws_elem.xpath(".//columns//field"):
            fname = field.get("name") or field.get("field")
            if fname:
                measures.add(fname.strip("[]"))
        
        # 4. LAST RESORT
        if not dimensions and not measures:
            for meta in instance_map.values():
                if meta["role"] == "dimension":
                    dimensions.add(meta["base_column"])
                elif meta["role"] == "measure":
                    measures.add(meta["base_column"])
        
        return sorted(list(dimensions)), sorted(list(measures))

    def _infer_visual_type(self, chart_type, dimensions, measures):
        """Infer visual type based on dimensions and measures (User provided logic)"""
        # Note: The user code uses `chart_type == "Automatic"`.
        # Our parser might return "automatic" or "unknown" or even VisualType enum values stringified.
        # We need to bridge that gap. Let's assume chart_type passed here is a string.
        
        # Normalize slightly for robustness, but stick close to user logic
        ct_str = str(chart_type)
        if ct_str == "Automatic" or ct_str == "automatic":
            geo_keywords = ["state", "province", "country", "city", "latitude", "longitude"]

            for dim in dimensions:
                if any(k in dim.lower() for k in geo_keywords):
                    return "Map"

            if dimensions and measures:
                return "Table"

            return "Automatic"
        else:
            return chart_type

    def _infer_axes(self, chart_type, dimensions, measures, column_map):
        columns = None
        rows = None
        
        if not dimensions and not measures:
            return {
                "columns": None,
                "rows": None,
            }
            
        # Simplified logic from reference
        # Note: Chart type detection in reference might return "Automatic", "Map", "Table"
        # In our parser we have visual_type returning ENUM strings like "bar", "text_table", etc.
        # We might need to map or strictly use the passed chart_type.
        
        # The reference logic:
        # if chart_type in ("Area", "Line", "Bar"):
        # We need to ensure chart_type is compatible.
        
        # Map our visual types to reference types if needed, or just check loosely
        ct_lower = str(chart_type).lower()
        is_cartesian = any(x in ct_lower for x in ["area", "line", "bar"])
        
        if is_cartesian:
            for meta in column_map.values():
                if meta.get("role") == "dimension" and meta.get("derivation") in ("Month", "Year", "Quarter"):
                    columns = f"{meta['derivation']}({meta['base_column']})"
                    break
            
            if not columns and dimensions:
                columns = dimensions[0]
                
            if measures:
                rows = f"SUM({measures[0]})"
                
        return {
            "columns": columns,
            "rows": rows,
        }

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
