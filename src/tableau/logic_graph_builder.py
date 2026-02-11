"""Logic Graph Builder - Construct dependency DAG from Tableau calculations"""
import re
from typing import List, Dict, Any, Set, Tuple
from dataclasses import dataclass
from enum import Enum
import networkx as nx
from loguru import logger

from src.tableau.twb_parser import CalculatedField, LODExpression, Worksheet, VisualType


@dataclass
class FieldDependency:
    """Metadata about a field dependency"""
    field_name: str
    field_type: str  # "BASE_COLUMN" | "CALCULATED_MEASURE" | "CALCULATED_COLUMN"
    original_role: str  # "measure" | "dimension" from Tableau
    is_aggregated: bool  # True if it's already an aggregate
    source_calc: 'CalculationNode' = None


class CalculationType(Enum):
    """Classification of calculation types"""
    MEASURE = "MEASURE"  # Aggregate calculation (SUM, AVG, etc.)
    CALCULATED_COLUMN = "CALCULATED_COLUMN"  # Row-level calculation
    LOD_EXPRESSION = "LOD_EXPRESSION"  # FIXED/INCLUDE/EXCLUDE
    TABLE_CALCULATION = "TABLE_CALCULATION"  # RANK, RUNNING_SUM, etc.
    PARAMETER = "PARAMETER"  # User parameter


class Granularity(Enum):
    """Calculation granularity"""
    ROW_LEVEL = "ROW_LEVEL"  # Evaluates per row
    AGGREGATE = "AGGREGATE"  # Evaluates at aggregated level
    TABLE = "TABLE"  # Table calculation (post-aggregation)


class ContextTransitionType(Enum):
    """
    Types of context transitions in Tableau → DAX conversion

    Component 2: Order of Operations
    """
    NONE = "NONE"  # No context shift
    FIXED_LOD = "FIXED_LOD"  # FIXED ignores view filters
    EXCLUDE_LOD = "EXCLUDE_LOD"  # EXCLUDE removes dimensions
    INCLUDE_LOD = "INCLUDE_LOD"  # INCLUDE adds dimensions
    CONTEXT_FILTER = "CONTEXT_FILTER"  # Context filters apply first
    TABLE_CALC = "TABLE_CALC"  # Post-aggregation calculation


@dataclass
class ContextTransition:
    """
    Metadata for context transitions (Component 2)

    Captures HOW evaluation context changes from Tableau to DAX.
    Critical for generating correct CALCULATE/ALLEXCEPT/ALL patterns.
    """
    transition_type: ContextTransitionType
    from_context: str  # Description of source context
    to_context: str  # Description of target context
    dax_pattern: str  # Recommended DAX pattern (ALLEXCEPT, ALL, CALCULATE, etc.)
    requires_allexcept: bool = False
    requires_all: bool = False
    requires_keepfilters: bool = False
    explanation: str = ""  # Human-readable explanation for LLM


@dataclass
class FilterContext:
    """Filter context for a calculation"""
    standard_filters: List[str]
    context_filters: List[str]  # Critical: context filters apply first
    is_context_dependent: bool


@dataclass
class VisualContext:
    """Visual context where calculation is used"""
    used_in_worksheets: List[str]
    visual_types: List[VisualType]  # NEW: What kind of visuals use this calc?
    partition_by: List[str]  # Grouping dimensions (like GROUP BY)
    sort_by: List[str]  # Sorting dimensions
    filters: FilterContext


@dataclass
class CalculationNode:
    """Node in the calculation dependency graph"""
    calc_id: str
    name: str
    formula: str
    calc_type: CalculationType
    granularity: Granularity
    depends_on: List[str]  # List of field names this calculation depends on
    dependency_level: int  # 0 = base field, 1 = depends on base, etc.
    visual_context: VisualContext
    is_lod: bool = False
    lod_type: str = None  # FIXED, INCLUDE, EXCLUDE
    context_transition: ContextTransition = None  # NEW: Component 2
    tableau_role: str = None  # "measure" or "dimension" from Tableau
    depends_on_metadata: Dict[str, FieldDependency] = None  # NEW: Dependency metadata

    def __post_init__(self):
        """Initialize mutable default values"""
        if self.depends_on_metadata is None:
            self.depends_on_metadata = {}


class LogicGraphBuilder:
    """
    Build dependency DAG from Tableau calculations

    Responsibilities:
    - Parse formulas to extract field references
    - Build directed acyclic graph (DAG) of dependencies
    - Topological sort to determine execution order
    - Classify calculation types (Measure vs. Column)
    - Detect granularity (row-level vs. aggregate)
    - Extract visual context for DAX generation
    """

    def __init__(self):
        self.graph = nx.DiGraph()
        self.calculations: Dict[str, CalculationNode] = {}
        self.base_fields: Set[str] = set()  # Non-calculated fields
        self.worksheets: List[Worksheet] = []
        self.field_roles: Dict[str, str] = {}  # NEW: Track role metadata (measure/dimension)

    def build_graph(
        self,
        calculated_fields: List[CalculatedField],
        lod_expressions: List[LODExpression],
        worksheets: List[Worksheet],
        base_field_names: Set[str]
    ) -> nx.DiGraph:
        """
        Build the dependency graph

        Args:
            calculated_fields: All calculated fields
            lod_expressions: All LOD expressions
            worksheets: All worksheets (for visual context)
            base_field_names: Set of non-calculated field names

        Returns:
            NetworkX directed graph
        """
        logger.info(f"Building logic graph from {len(calculated_fields)} calculations")

        self.base_fields = base_field_names
        self.worksheets = worksheets

        if not base_field_names:
            logger.error(f"⚠️  CRITICAL: base_field_names is EMPTY! All fields will be UNKNOWN!")

        # Build role map from calculated fields
        for calc in calculated_fields:
            self.field_roles[calc.name] = calc.role  # "measure" or "dimension"

        # Step 1: Create nodes for all calculations
        for calc in calculated_fields:
            self._add_calculation_node(calc)

        # Step 2: Mark LOD expressions
        for lod in lod_expressions:
            if lod.name in self.calculations:
                node = self.calculations[lod.name]
                node.is_lod = True
                node.lod_type = lod.lod_type
                node.calc_type = CalculationType.LOD_EXPRESSION

        # Step 3: Extract dependencies and build edges with metadata
        for calc_name, node in self.calculations.items():
            dependencies = self._extract_dependencies(node.formula)
            node.depends_on = dependencies

            # Build dependency metadata
            depends_on_metadata = {}
            for dep in dependencies:
                if dep in self.base_fields:
                    # Base column from data source
                    depends_on_metadata[dep] = FieldDependency(
                        field_name=dep,
                        field_type="BASE_COLUMN",
                        original_role="dimension",  # Base fields are typically dimensions
                        is_aggregated=False,
                        source_calc=None
                    )
                elif dep in self.calculations:
                    # Reference to another calculation
                    dep_calc = self.calculations[dep]

                    # Determine if it's a measure or calculated column
                    if dep_calc.calc_type == CalculationType.MEASURE:
                        field_type = "CALCULATED_MEASURE"
                        is_aggregated = True
                    else:
                        field_type = "CALCULATED_COLUMN"
                        is_aggregated = False

                    depends_on_metadata[dep] = FieldDependency(
                        field_name=dep,
                        field_type=field_type,
                        original_role=self.field_roles.get(dep, "unknown"),
                        is_aggregated=is_aggregated,
                        source_calc=dep_calc
                    )
                else:
                    # Unknown field - conservative fallback
                    depends_on_metadata[dep] = FieldDependency(
                        field_name=dep,
                        field_type="UNKNOWN",
                        original_role="unknown",
                        is_aggregated=False,
                        source_calc=None
                    )
                    logger.warning(f"❌ {dep} → UNKNOWN (not in base_fields or calculations!)")

            # Store metadata in node
            node.depends_on_metadata = depends_on_metadata

            # Add edges: dependency -> calculation
            for dep in dependencies:
                if dep in self.calculations:
                    # Dependency is another calculation
                    self.graph.add_edge(dep, calc_name)
                else:
                    # Dependency is a base field
                    if dep not in self.graph:
                        self.graph.add_node(dep, type="base_field")
                    self.graph.add_edge(dep, calc_name)

        # Step 4: Calculate dependency levels
        self._calculate_dependency_levels()

        # Step 5: Extract visual context
        self._extract_visual_contexts()

        # Step 6: NEW - Analyze context transitions (Component 2)
        self._analyze_context_transitions()

        logger.info(f"Built graph with {self.graph.number_of_nodes()} nodes and {self.graph.number_of_edges()} edges")
        return self.graph

    def _add_calculation_node(self, calc: CalculatedField):
        """Add a calculation node to the graph"""
        # Classify calculation type
        calc_type = self._classify_calculation_type(calc)
        granularity = self._detect_granularity(calc)

        node = CalculationNode(
            calc_id=calc.name,
            name=calc.name,
            formula=calc.formula,
            calc_type=calc_type,
            granularity=granularity,
            depends_on=[],
            dependency_level=0,
            visual_context=VisualContext(
                used_in_worksheets=[],
                visual_types=[],  # NEW
                partition_by=[],
                sort_by=[],
                filters=FilterContext([], [], False)
            ),
            tableau_role=calc.role  # NEW: Preserve Tableau role
        )

        self.calculations[calc.name] = node
        self.graph.add_node(calc.name, **node.__dict__)

    def _classify_calculation_type(self, calc: CalculatedField) -> CalculationType:
        """Classify the type of calculation"""
        formula = calc.formula.upper()

        # Table calculations
        table_calc_keywords = [
            'WINDOW_', 'RUNNING_', 'INDEX()', 'RANK', 'LOOKUP',
            'PREVIOUS_VALUE', 'SIZE()', 'FIRST()', 'LAST()'
        ]
        if any(kw in formula for kw in table_calc_keywords):
            return CalculationType.TABLE_CALCULATION

        # Aggregations = measures
        agg_keywords = ['SUM(', 'AVG(', 'COUNT(', 'MIN(', 'MAX(', 'STDEV(', 'VAR(']
        if any(kw in formula for kw in agg_keywords):
            return CalculationType.MEASURE

        # LOD expressions
        if re.search(r'\{(FIXED|INCLUDE|EXCLUDE)', formula, re.IGNORECASE):
            return CalculationType.LOD_EXPRESSION

        # Default: calculated column (row-level)
        return CalculationType.CALCULATED_COLUMN

    def _detect_granularity(self, calc: CalculatedField) -> Granularity:
        """Detect calculation granularity"""
        formula = calc.formula.upper()

        # Table calculations operate post-aggregation
        table_calc_keywords = ['WINDOW_', 'RUNNING_', 'INDEX()', 'RANK']
        if any(kw in formula for kw in table_calc_keywords):
            return Granularity.TABLE

        # Aggregations
        if any(agg in formula for agg in ['SUM(', 'AVG(', 'COUNT(', 'MIN(', 'MAX(']):
            return Granularity.AGGREGATE

        # Default: row-level
        return Granularity.ROW_LEVEL

    def _extract_dependencies(self, formula: str) -> List[str]:
        """
        Extract field references from formula

        Tableau field references are wrapped in [brackets]

        Example:
            "SUM([Sales]) / SUM([Profit])" -> ["Sales", "Profit"]
        """
        # Pattern: [FieldName]
        pattern = r'\[([^\]]+)\]'
        matches = re.findall(pattern, formula)

        # Remove duplicates and clean
        dependencies = []
        for match in matches:
            cleaned = match.strip()
            if cleaned and cleaned not in dependencies:
                dependencies.append(cleaned)

        return dependencies

    def _calculate_dependency_levels(self):
        """
        Calculate dependency level for each calculation

        Level 0: Base fields (no dependencies)
        Level 1: Depends only on base fields
        Level 2: Depends on level 1 calculations
        etc.
        """
        # Topological sort to get execution order
        try:
            sorted_nodes = list(nx.topological_sort(self.graph))
        except nx.NetworkXError:
            logger.error("Calculation graph has cycles! Cannot determine execution order.")
            return

        # Assign levels
        for node_name in sorted_nodes:
            if node_name in self.base_fields:
                # Base field = level 0
                continue

            if node_name not in self.calculations:
                # Base field node
                continue

            node = self.calculations[node_name]

            # Level = max(dependency levels) + 1
            dep_levels = []
            for dep in node.depends_on:
                if dep in self.calculations:
                    dep_levels.append(self.calculations[dep].dependency_level)
                else:
                    # Base field = level 0
                    dep_levels.append(0)

            node.dependency_level = max(dep_levels, default=0) + 1

        logger.debug("Calculated dependency levels")

    def _extract_visual_contexts(self):
        """Extract visual context for each calculation from worksheets"""
        for ws in self.worksheets:
            # Get all fields used in this worksheet
            all_fields = ws.rows_fields + ws.columns_fields + ws.marks_fields

            for field in all_fields:
                if field in self.calculations:
                    node = self.calculations[field]

                    # Add worksheet
                    if ws.name not in node.visual_context.used_in_worksheets:
                        node.visual_context.used_in_worksheets.append(ws.name)

                    # NEW: Add visual type
                    if ws.visual_type not in node.visual_context.visual_types:
                        node.visual_context.visual_types.append(ws.visual_type)

                    # Determine partition (grouping) dimensions
                    # These are the dimensions in rows/columns (excluding the calc itself)
                    partition_dims = [
                        f for f in ws.rows_fields + ws.columns_fields
                        if f != field and f not in self.calculations  # Exclude other calcs
                    ]

                    for dim in partition_dims:
                        if dim not in node.visual_context.partition_by:
                            node.visual_context.partition_by.append(dim)

        logger.debug("Extracted visual contexts")

    # ============================================
    # Query Methods
    # ============================================

    def get_execution_order(self) -> List[str]:
        """
        Get calculations in execution order (topological sort)

        Returns:
            List of calculation names in dependency order
        """
        try:
            sorted_nodes = list(nx.topological_sort(self.graph))
            # Filter to only calculations (exclude base fields)
            calc_order = [n for n in sorted_nodes if n in self.calculations]
            return calc_order
        except nx.NetworkXError:
            logger.error("Graph has cycles!")
            return list(self.calculations.keys())

    def get_calculation_node(self, calc_name: str) -> CalculationNode:
        """Get calculation node by name"""
        return self.calculations.get(calc_name)

    def get_dependencies(self, calc_name: str) -> List[str]:
        """Get direct dependencies of a calculation"""
        if calc_name in self.calculations:
            return self.calculations[calc_name].depends_on
        return []

    def get_dependents(self, calc_name: str) -> List[str]:
        """Get calculations that depend on this one"""
        if calc_name not in self.graph:
            return []

        return list(self.graph.successors(calc_name))

    def get_root_calculations(self) -> List[str]:
        """Get calculations with no dependencies (level 0/1)"""
        roots = []
        for name, node in self.calculations.items():
            if node.dependency_level <= 1:
                roots.append(name)
        return roots

    def get_lod_expressions(self) -> List[CalculationNode]:
        """Get all LOD expression nodes"""
        return [node for node in self.calculations.values() if node.is_lod]

    def get_table_calculations(self) -> List[CalculationNode]:
        """Get all table calculation nodes"""
        return [
            node for node in self.calculations.values()
            if node.calc_type == CalculationType.TABLE_CALCULATION
        ]

    # ============================================
    # Export Methods
    # ============================================

    def to_dict(self) -> Dict[str, Any]:
        """Export graph as dictionary for JSON serialization"""
        nodes = []
        for calc_name, node in self.calculations.items():
            nodes.append({
                "id": node.calc_id,
                "name": node.name,
                "formula": node.formula,
                "type": node.calc_type.value,
                "granularity": node.granularity.value,
                "dependency_level": node.dependency_level,
                "depends_on": node.depends_on,
                "is_lod": node.is_lod,
                "lod_type": node.lod_type,
                "visual_context": {
                    "used_in": node.visual_context.used_in_worksheets,
                    "partition_by": node.visual_context.partition_by,
                    "sort_by": node.visual_context.sort_by
                }
            })

        edges = []
        for source, target in self.graph.edges():
            edges.append({"source": source, "target": target})

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "total_calculations": len(self.calculations),
                "total_dependencies": self.graph.number_of_edges(),
                "max_dependency_level": max(
                    (node.dependency_level for node in self.calculations.values()),
                    default=0
                ),
                "lod_count": len(self.get_lod_expressions()),
                "table_calc_count": len(self.get_table_calculations())
            }
        }

    def export_for_reactflow(self) -> Dict[str, Any]:
        """
        Export graph in ReactFlow format

        Returns:
            Dictionary with nodes and edges for ReactFlow visualization
        """
        reactflow_nodes = []
        reactflow_edges = []

        # Auto-layout using hierarchical positioning
        pos = nx.spring_layout(self.graph, k=2, iterations=50)

        # Create nodes
        for calc_name, node in self.calculations.items():
            x, y = pos.get(calc_name, (0, 0))

            # Determine node color based on type
            node_colors = {
                CalculationType.MEASURE: "#f59e0b",  # orange
                CalculationType.CALCULATED_COLUMN: "#3b82f6",  # blue
                CalculationType.LOD_EXPRESSION: "#8b5cf6",  # purple
                CalculationType.TABLE_CALCULATION: "#ec4899",  # pink
                CalculationType.PARAMETER: "#10b981"  # green
            }

            reactflow_nodes.append({
                "id": calc_name,
                "type": "calculationNode",
                "data": {
                    "label": node.name,
                    "formula": node.formula[:50] + "..." if len(node.formula) > 50 else node.formula,
                    "calcType": node.calc_type.value,
                    "level": node.dependency_level,
                    "isLOD": node.is_lod
                },
                "position": {"x": x * 500, "y": y * 500},
                "style": {
                    "background": node_colors.get(node.calc_type, "#6b7280"),
                    "color": "white",
                    "border": "2px solid" if node.is_lod else "1px solid",
                    "borderColor": "#8b5cf6" if node.is_lod else "#d1d5db"
                }
            })

        # Create edges
        for source, target in self.graph.edges():
            if source in self.calculations and target in self.calculations:
                reactflow_edges.append({
                    "id": f"{source}-{target}",
                    "source": source,
                    "target": target,
                    "type": "smoothstep",
                    "animated": False
                })

        return {
            "nodes": reactflow_nodes,
            "edges": reactflow_edges
        }

    # ============================================
    # Component 2: Context Transition Analysis
    # ============================================

    def _analyze_context_transitions(self):
        """
        Analyze context transitions for all calculations (Component 2)

        Determines HOW evaluation context shifts from Tableau to DAX.
        Critical for generating correct CALCULATE/ALLEXCEPT/ALL patterns.
        """
        for calc_name, node in self.calculations.items():
            transition = self._determine_context_transition(node)
            node.context_transition = transition

            if transition.transition_type != ContextTransitionType.NONE:
                logger.debug(f"Context transition for '{calc_name}': {transition.transition_type.value}")

    def _determine_context_transition(self, node: CalculationNode) -> ContextTransition:
        """
        Determine context transition type for a calculation

        Returns:
            ContextTransition with metadata for DAX generation
        """
        # Pattern 1: FIXED LOD
        if node.is_lod and node.lod_type == "FIXED":
            return self._create_fixed_lod_transition(node)

        # Pattern 2: EXCLUDE LOD
        if node.is_lod and node.lod_type == "EXCLUDE":
            return self._create_exclude_lod_transition(node)

        # Pattern 3: INCLUDE LOD
        if node.is_lod and node.lod_type == "INCLUDE":
            return self._create_include_lod_transition(node)

        # Pattern 4: Context filters
        if node.visual_context.filters.context_filters:
            return self._create_context_filter_transition(node)

        # Pattern 5: Table calculations
        if node.calc_type == CalculationType.TABLE_CALCULATION:
            return self._create_table_calc_transition(node)

        # Default: No context transition
        return ContextTransition(
            transition_type=ContextTransitionType.NONE,
            from_context="View context",
            to_context="View context",
            dax_pattern="Standard measure",
            explanation="No context shift - standard aggregation"
        )

    def _create_fixed_lod_transition(self, node: CalculationNode) -> ContextTransition:
        """
        Create transition metadata for FIXED LOD

        FIXED ignores view filters except context filters.
        DAX Pattern: CALCULATE with ALLEXCEPT
        """
        partition = node.visual_context.partition_by

        if partition:
            dax_pattern = f"CALCULATE(expr, ALLEXCEPT(Table, {', '.join(partition)}))"
            explanation = f"FIXED LOD ignores view filters. Keep only dimensions: {', '.join(partition)}"
        else:
            dax_pattern = "CALCULATE(expr, ALL(Table))"
            explanation = "FIXED LOD with no dimensions = grand total (ignore all filters)"

        return ContextTransition(
            transition_type=ContextTransitionType.FIXED_LOD,
            from_context="View context (with filters)",
            to_context=f"Fixed context ({', '.join(partition) if partition else 'Grand total'})",
            dax_pattern=dax_pattern,
            requires_allexcept=bool(partition),
            requires_all=not bool(partition),
            explanation=explanation
        )

    def _create_exclude_lod_transition(self, node: CalculationNode) -> ContextTransition:
        """
        Create transition metadata for EXCLUDE LOD

        EXCLUDE removes specific dimensions from grouping.
        DAX Pattern: CALCULATE with ALL(excluded dimensions)
        """
        # Extract excluded dimensions from formula
        # Example: {EXCLUDE [Region]: SUM([Sales])} -> excludes Region
        formula_upper = node.formula.upper()
        match = re.search(r'EXCLUDE\s+\[([^\]]+)\]', formula_upper)
        excluded = match.group(1) if match else "Unknown"

        dax_pattern = f"CALCULATE(expr, ALL(Table[{excluded}]))"
        explanation = f"EXCLUDE removes {excluded} from grouping. Use ALL() to ignore that dimension."

        return ContextTransition(
            transition_type=ContextTransitionType.EXCLUDE_LOD,
            from_context=f"View context (including {excluded})",
            to_context=f"View context (excluding {excluded})",
            dax_pattern=dax_pattern,
            requires_all=True,
            explanation=explanation
        )

    def _create_include_lod_transition(self, node: CalculationNode) -> ContextTransition:
        """
        Create transition metadata for INCLUDE LOD

        INCLUDE adds dimensions to grouping.
        No direct DAX equivalent - requires restructuring.
        """
        formula_upper = node.formula.upper()
        match = re.search(r'INCLUDE\s+\[([^\]]+)\]', formula_upper)
        included = match.group(1) if match else "Unknown"

        explanation = f"INCLUDE adds {included} to grouping. No direct DAX equivalent - consider adding dimension to visual or using SUMMARIZE."

        return ContextTransition(
            transition_type=ContextTransitionType.INCLUDE_LOD,
            from_context="View context",
            to_context=f"View context (with added {included})",
            dax_pattern="SUMMARIZE or calculated table",
            explanation=explanation
        )

    def _create_context_filter_transition(self, node: CalculationNode) -> ContextTransition:
        """
        Create transition metadata for context filters

        Context filters apply BEFORE standard filters.
        DAX Pattern: KEEPFILTERS or ALLSELECTED
        """
        context_filters = node.visual_context.filters.context_filters
        filters_str = ", ".join(context_filters)

        dax_pattern = f"CALCULATE(expr, KEEPFILTERS(Table[{context_filters[0]}] = value))"
        explanation = f"Context filters ({filters_str}) apply first. Use KEEPFILTERS to preserve filter order."

        return ContextTransition(
            transition_type=ContextTransitionType.CONTEXT_FILTER,
            from_context="View context",
            to_context=f"Context-filtered ({filters_str})",
            dax_pattern=dax_pattern,
            requires_keepfilters=True,
            explanation=explanation
        )

    def _create_table_calc_transition(self, node: CalculationNode) -> ContextTransition:
        """
        Create transition metadata for table calculations

        Table calculations run AFTER aggregation.
        Often require model changes (Index columns, etc.)
        """
        explanation = "Table calculation runs post-aggregation. May require Power BI model changes (Index columns, Date table, etc.)"

        return ContextTransition(
            transition_type=ContextTransitionType.TABLE_CALC,
            from_context="Aggregated values",
            to_context="Table calculation result",
            dax_pattern="Calculated column or model enhancement",
            explanation=explanation
        )
