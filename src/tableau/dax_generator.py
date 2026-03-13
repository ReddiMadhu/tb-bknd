"""DAX Generator - AI-powered Tableau-to-DAX conversion using LLM"""
import json
import re
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from loguru import logger

from src.llm_reasoner import LLMReasoner
from src.tableau.pattern_loader import PatternLoader, ConversionPattern
from src.tableau.logic_graph_builder import CalculationNode


@dataclass
class DAXResult:
    """Result of DAX conversion"""
    dax_formula: str
    reasoning: str
    confidence: float
    method: str  # LLM_PATTERN, LLM_GENERATED, RULE_BASED
    warnings: List[str]
    pattern_used: Optional[str] = None


class DAXGenerator:
    """
    AI-powered DAX generator using LLM with pattern library
    """

    def __init__(self, patterns_file: str = "data/conversion_patterns/patterns.yaml"):
        """Initialize DAX generator"""
        self.pattern_loader = PatternLoader(patterns_file)
        self.llm_reasoner = LLMReasoner()
        self.all_patterns = self.pattern_loader.get_all_patterns()

        logger.info(f"DAX Generator initialized with {len(self.all_patterns)} patterns")

    def tableau_to_dax(
        self,
        calc_node: CalculationNode,
        data_profile: Optional[Dict[str, Any]] = None,
        table_name: str = "Sales",
        source_table_map: Optional[Dict[str, str]] = None,
        known_measures: Optional[set] = None,
    ) -> DAXResult:
        """
        Convert Tableau calculation to DAX

        Args:
            calc_node: Calculation node with formula and context
            data_profile: Data statistics (row count, cardinality, etc.)
            table_name: Default/fallback table name for DAX references
            source_table_map: Maps Tableau source qualifier names to actual Power BI table names
            known_measures: Set of calc names that are already computed measures

        Returns:
            DAXResult with generated DAX and metadata
        """
        logger.info(f"Converting calculation: {calc_node.name}")

        return self._generate_with_llm(
            calc_node, data_profile, table_name,
            source_table_map=source_table_map or {},
            known_measures=known_measures or set()
        )

    def _build_table_context(
        self,
        original_formula: str,
        source_table_map: Dict[str, str]
    ) -> tuple:
        """
        Build table context from formula and source map.

        Returns:
            (tables_info_text, field_to_table_dict)
        """
        # Extract [Field (Source)] qualifiers from formula
        table_refs = {}
        for match in re.finditer(r'\[([^\]\(]+?)\s+\(([^\)]+)\)\]', original_formula):
            field_name = match.group(1).strip()
            source_name = match.group(2).strip()
            if source_name in source_table_map:
                table_refs[field_name] = source_table_map[source_name]

        # Build tables info line
        if source_table_map:
            unique_tables = sorted(set(source_table_map.values()))
            tables_info = f"\nPower BI Tables: {', '.join(unique_tables)}"
        else:
            tables_info = ""

        return tables_info, table_refs

    def _apply_if_to_calculate(self, result: DAXResult, calc_node: CalculationNode) -> DAXResult:
        """
        Post-processor: Convert IF patterns to CALCULATE for physical columns.

        Pattern: IF(Table[col] = "val", Table[Amount], BLANK())
        → CALCULATE(SUM(Table[Amount]), Table[col] = "val")

        Only applies when return expression is a qualified table column (not a measure).
        """
        if not result or not result.dax_formula:
            return result

        dax = result.dax_formula.strip()

        # Extract "Name = " prefix
        name_prefix = ""
        formula_part = dax
        eq_match = re.match(r'^([A-Za-z0-9_\s%]+)\s*=\s*(.+)$', dax, re.DOTALL)
        if eq_match:
            name_prefix = f"{eq_match.group(1).strip()} = "
            formula_part = eq_match.group(2).strip()

        # Match: IF(condition, Table[Column], BLANK()/0)
        # Anchored so it matches the whole formula (not embedded IFs with extra args)
        match = re.search(
            r'^\s*IF\s*\(\s*([^,]+?)\s*,\s*([A-Za-z0-9_\'"\[\]\.\s]+?)\s*,\s*(?:BLANK\s*\(\s*\)|0)\s*\)\s*$',
            formula_part, re.IGNORECASE
        )

        if match:
            condition = match.group(1).strip()
            return_expr = match.group(2).strip()

            # Only convert if it's Table[Column] format, not bare [Measure]
            if re.match(r'^[A-Za-z_][A-Za-z0-9_\s]*\[[^\]]+\]$', return_expr):
                new_formula = f"CALCULATE(SUM({return_expr}), {condition})"
                result.dax_formula = f"{name_prefix}{new_formula}"
                result.reasoning += "\nPost-processed: Converted IF() to CALCULATE(SUM())."
                logger.info(f"Converted IF to CALCULATE: {calc_node.name}")

        return result

    def _generate_with_llm(
        self,
        calc_node: CalculationNode,
        data_profile: Optional[Dict[str, Any]],
        table_name: str,
        source_table_map: Optional[Dict[str, str]] = None,
        known_measures: Optional[set] = None,
    ) -> DAXResult:
        """LLM-based DAX conversion with focused prompt."""
        import time

        source_table_map = source_table_map or {}
        known_measures = known_measures or set()

        # Clean formula: strip [Field (Source)] → [Field]
        original_formula = calc_node.formula
        clean_formula = re.sub(r'\[([^\]\(]+?)\s+\([^\)]+\)\]', r'[\1]', original_formula)
        print("clean_formula", clean_formula)

        # Build context
        tables_info, table_refs = self._build_table_context(original_formula, source_table_map)

        # Field-to-table mapping block
        if table_refs:
            mapping_lines = [f"  {field} → {tbl}[{field}]" for field, tbl in sorted(table_refs.items())]
            table_mapping = "\nField mappings:\n" + "\n".join(mapping_lines)
        else:
            table_mapping = ""

        # Known measures block
        if known_measures:
            measures_list = ", ".join(f"[{m}]" for m in sorted(known_measures))
            measures_info = f"\nKnown measures (use [Name] only, no table, no SUM): {measures_list}"
        else:
            measures_info = ""

        prompt = f"""Convert Tableau formula to DAX.

MEASURE NAME: {calc_node.name}
TABLEAU FORMULA: {clean_formula}
{tables_info}{table_mapping}{measures_info}

CONVERSION RULES:
1. Known measures: Reference as [MeasureName] - NO table prefix, NO SUM()
2. Physical columns: Use Table[ColumnName] with SUM/AVG/COUNT/etc.
3. IF patterns:
   - IF Table[col] = "value" THEN Table[Amount] END → CALCULATE(SUM(Table[Amount]), Table[col] = "value")
   - IF condition THEN "text" END → IF(condition, "text", BLANK())
   - IF condition THEN [Measure] END → CALCULATE([Measure], condition)
4. Arithmetic:
   - Division: DIVIDE(a, b, 0) not a/b
   - Percentage: DIVIDE(a, b, 0) * 100
5. Fields with NO table qualifier in the Tableau formula (e.g. [Cross sell bugdet], [New Budget]) are DAX MEASURES — reference as [FieldName] with NO SUM and NO table prefix
6. CRITICAL: Output MUST start with measure name. Format: MeasureName = <formula>

OUTPUT FORMAT (valid JSON only, no markdown):
{{"dax_formula": "{calc_node.name} = <complete formula>", "reasoning": "<1-2 sentence explanation>", "confidence": 0.9}}

Examples:
- {{"dax_formula": "Total Sales = SUM(Sales[Amount])", "reasoning": "Simple sum aggregation", "confidence": 0.95}}
- {{"dax_formula": "Active Amount = CALCULATE(SUM(Sales[Amount]), Sales[Status] = \\"Active\\")", "reasoning": "IF filter converted to CALCULATE", "confidence": 0.9}}
- {{"dax_formula": "Growth % = DIVIDE([Current Sales], [Prior Sales], 0) * 100", "reasoning": "Both are known measures, referenced directly", "confidence": 0.9}}"""

        logger.debug(f"Converting: {calc_node.name}")

        try:
            time.sleep(4.1)  # Rate limiting
            response = self.llm_reasoner.reason(prompt)
            result = self._parse_llm_response(response, clean_formula, calc_node.name)

            # Ensure measure name prefix is present
            if result and result.dax_formula and '=' not in result.dax_formula:
                result.dax_formula = f"{calc_node.name} = {result.dax_formula}"

            # Post-process IF → CALCULATE
            if result and result.dax_formula:
                result = self._apply_if_to_calculate(result, calc_node)

            print("dax_formula", result.dax_formula if result else "")
            return result

        except Exception as e:
            logger.error(f"LLM conversion failed for {calc_node.name}: {e}")
            return self._fallback_conversion(calc_node, table_name, source_table_map, table_refs)

    def _parse_llm_response(
        self,
        response: str,
        original_formula: str,
        calc_name: str
    ) -> DAXResult:
        """Parse LLM JSON response."""
        try:
            cleaned = response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            data = json.loads(cleaned)
            dax_formula = data.get("dax_formula", "").strip()
            print("dax_formula", dax_formula)

            # Guard: ensure measure name is present
            if dax_formula and '=' not in dax_formula:
                dax_formula = f"{calc_name} = {dax_formula}"

            return DAXResult(
                dax_formula=dax_formula,
                reasoning=data.get("reasoning", ""),
                confidence=float(data.get("confidence", 0.7)),
                method="LLM_PATTERN",
                warnings=data.get("warnings", []),
                pattern_used=data.get("pattern_used")
            )

        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error for {calc_name}: {e}")

            dax_match = re.search(r'(?:dax_formula["\s:]+|=\s*)([^"]+)', response)
            if dax_match:
                dax = dax_match.group(1).strip()
                if '=' not in dax:
                    dax = f"{calc_name} = {dax}"
            else:
                dax = f"{calc_name} = -- PARSE ERROR"

            return DAXResult(
                dax_formula=dax,
                reasoning="Failed to parse LLM response",
                confidence=0.5,
                method="LLM_GENERATED",
                warnings=["JSON parse error - review required"]
            )

    def _fallback_conversion(
        self,
        calc_node: CalculationNode,
        table_name: str,
        source_table_map: Dict[str, str] = None,
        table_refs: Dict[str, str] = None,
    ) -> DAXResult:
        """Fallback when LLM fails — uses table_refs if available."""
        source_table_map = source_table_map or {}
        table_refs = table_refs or {}

        formula = re.sub(r'\[([^\]\(]+?)\s+\([^\)]+\)\]', r'[\1]', calc_node.formula)

        def replace_field(match):
            field = match.group(1)
            if field in table_refs:
                return f"{table_refs[field]}[{field}]"
            return f"{table_name}[{field}]"

        dax = re.sub(r'\[([^\]]+)\]', replace_field, formula)
        dax = re.sub(r'(\S+)\s*/\s*(\S+)', r'DIVIDE(\1, \2, 0)', dax)

        return DAXResult(
            dax_formula=f"{calc_node.name} = {dax}",
            reasoning="LLM failed — rule-based fallback with table context",
            confidence=0.5,
            method="RULE_BASED",
            warnings=["LLM conversion failed - manual review required"]
        )

    # ============================================
    # Specialized Methods
    # ============================================

    def convert_lod_expression(
        self,
        lod_type: str,
        dimensions: List[str],
        aggregation_formula: str,
        table_name: str
    ) -> DAXResult:
        """Convert LOD expression to DAX"""
        logger.info(f"Converting LOD: {lod_type} on {dimensions}")

        if lod_type == "FIXED":
            if dimensions:
                dim_refs = [f"{table_name}[{dim}]" for dim in dimensions]
                allexcept = f"ALLEXCEPT({table_name}, {', '.join(dim_refs)})"
                dax = f"CALCULATE(\n    {aggregation_formula},\n    {allexcept}\n)"
            else:
                dax = f"CALCULATE(\n    {aggregation_formula},\n    ALL({table_name})\n)"

            return DAXResult(
                dax_formula=dax,
                reasoning=f"FIXED LOD using ALLEXCEPT: {dimensions}",
                confidence=0.90, method="RULE_BASED", warnings=[],
                pattern_used="fixed_lod"
            )

        elif lod_type == "EXCLUDE":
            all_dims = [f"ALL({table_name}[{dim}])" for dim in dimensions]
            dax = f"CALCULATE(\n    {aggregation_formula},\n    {', '.join(all_dims)}\n)"

            return DAXResult(
                dax_formula=dax,
                reasoning=f"EXCLUDE LOD using ALL: {dimensions}",
                confidence=0.85, method="RULE_BASED", warnings=[],
                pattern_used="exclude_lod"
            )

        elif lod_type == "INCLUDE":
            return DAXResult(
                dax_formula=f"-- INCLUDE LOD: {aggregation_formula}",
                reasoning="INCLUDE LOD has no direct DAX equivalent",
                confidence=0.60, method="RULE_BASED",
                warnings=["Requires redesign - add dimension to visual or use SUMMARIZE"],
                pattern_used="include_lod"
            )

        return self._fallback_conversion(
            CalculationNode(
                calc_id="unknown",
                name="LOD Expression",
                formula=f"{{{lod_type} {dimensions}: {aggregation_formula}}}",
                calc_type=None, granularity=None, depends_on=[],
                dependency_level=0, visual_context=None
            ),
            table_name
        )

    def convert_parameter(
        self,
        parameter_name: str,
        datatype: str,
        allowable_values: List[Any],
        table_name: str = "Parameters"
    ) -> DAXResult:
        """Convert Tableau parameter to Power BI disconnected table"""
        values_str = ", ".join([f'"{v}"' if isinstance(v, str) else str(v) for v in allowable_values])

        dax = f"""{parameter_name} =
SELECTEDVALUE(
    '{table_name}'[{parameter_name}],
    "{allowable_values[0] if allowable_values else 'Default'}"
)"""

        instructions = f"""
-- PARAMETER SETUP --
1. Create table in Power Query: {table_name}
2. Add column: {parameter_name}  Values: {values_str}
3. Add slicer using {table_name}[{parameter_name}]
4. Use measure below to get selected value:
{dax}
"""
        return DAXResult(
            dax_formula=dax,
            reasoning=instructions,
            confidence=0.85, method="RULE_BASED",
            warnings=["Requires disconnected parameter table"],
            pattern_used="parameter_single_value"
        )
