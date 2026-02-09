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

    Design Philosophy:
    - Pass ALL patterns directly to LLM (no vector DB)
    - Use chain-of-thought reasoning
    - Provide rich context (visual, data profile)
    - Generate optimized, production-ready DAX
    """

    def __init__(self, patterns_file: str = "data/conversion_patterns/patterns.yaml"):
        """
        Initialize DAX generator

        Args:
            patterns_file: Path to pattern YAML file
        """
        self.pattern_loader = PatternLoader(patterns_file)
        self.llm_reasoner = LLMReasoner()
        self.all_patterns = self.pattern_loader.get_all_patterns()

        logger.info(f"DAX Generator initialized with {len(self.all_patterns)} patterns")

    # ============================================
    # Main Conversion Method
    # ============================================

    def tableau_to_dax(
        self,
        calc_node: CalculationNode,
        data_profile: Optional[Dict[str, Any]] = None,
        table_name: str = "Sales"
    ) -> DAXResult:
        """
        Convert Tableau calculation to DAX

        Args:
            calc_node: Calculation node with formula and context
            data_profile: Data statistics (row count, cardinality, etc.)
            table_name: Default table name for DAX references

        Returns:
            DAXResult with generated DAX and metadata
        """
        logger.info(f"Converting calculation: {calc_node.name}")

        # Step 1: Check for exact pattern match (fast path)
        pattern_match = self._find_exact_pattern_match(calc_node.formula)

        if pattern_match and pattern_match.confidence >= 0.99:
            logger.debug(f"Exact pattern match found: {pattern_match.pattern_id}")
            return self._apply_pattern(pattern_match, calc_node, table_name)

        # Step 2: LLM-powered conversion with all patterns
        llm_result = self._generate_with_llm(calc_node, data_profile, table_name)

        return llm_result

    def _find_exact_pattern_match(self, tableau_formula: str) -> Optional[ConversionPattern]:
        """Find exact pattern match by formula similarity"""
        best_match = self.pattern_loader.find_best_match(
            tableau_formula,
            threshold=0.95  # Very high threshold for exact match
        )

        return best_match

    def _apply_pattern(
        self,
        pattern: ConversionPattern,
        calc_node: CalculationNode,
        table_name: str
    ) -> DAXResult:
        """
        Apply a pattern directly (rule-based conversion)

        Args:
            pattern: Matched pattern
            calc_node: Calculation node
            table_name: Table name for DAX

        Returns:
            DAXResult with applied pattern
        """
        # Simple substitution for exact matches
        dax = pattern.dax

        # Replace table placeholder if present
        dax = dax.replace("Sales[", f"{table_name}[")

        return DAXResult(
            dax_formula=dax,
            reasoning=f"Exact pattern match: {pattern.pattern_id}. {pattern.notes}",
            confidence=pattern.confidence,
            method="RULE_BASED",
            warnings=[],
            pattern_used=pattern.pattern_id
        )

    def _generate_with_llm(
        self,
        calc_node: CalculationNode,
        data_profile: Optional[Dict[str, Any]],
        table_name: str
    ) -> DAXResult:
        """
        Generate DAX using LLM with pattern library

        This is the core AI-powered conversion.
        """
        # Prepare context
        visual_context = {
            "used_in_worksheets": calc_node.visual_context.used_in_worksheets,
            "visual_types": [vt.value for vt in calc_node.visual_context.visual_types],
            "partition_by": calc_node.visual_context.partition_by,
            "sort_by": calc_node.visual_context.sort_by,
            "filters": [],
            # NEW: Component 2 - Context Transition
            "context_transition": self._context_transition_to_dict(calc_node.context_transition) if calc_node.context_transition else None
        }

        # Prepare pattern library for prompt
        patterns_json = json.dumps(
            self.pattern_loader.to_dict_list(),
            indent=2
        )

        # Build prompt
        prompt = self._build_conversion_prompt(
            tableau_formula=calc_node.formula,
            calc_name=calc_node.name,
            calc_type=calc_node.calc_type.value,
            visual_context=visual_context,
            data_profile=data_profile or {},
            patterns_json=patterns_json,
            table_name=table_name
        )

        # Call LLM
        logger.debug("Calling LLM for DAX generation...")

        try:
            response = self.llm_reasoner.reason(prompt)

            # Parse response (expected JSON)
            result = self._parse_llm_response(response, calc_node.formula)

            return result

        except Exception as e:
            logger.error(f"LLM conversion failed: {e}")

            # Fallback: basic conversion
            return self._fallback_conversion(calc_node, table_name)

    def _build_conversion_prompt(
        self,
        tableau_formula: str,
        calc_name: str,
        calc_type: str,
        visual_context: Dict[str, Any],
        data_profile: Dict[str, Any],
        patterns_json: str,
        table_name: str
    ) -> str:
        """Build the LLM prompt for DAX generation with full visual context"""

        # Interpret visual types for LLM
        visual_types = visual_context.get('visual_types', [])
        visual_hint = self._get_visual_context_hint(visual_types, visual_context)

        # NEW: Context transition guidance (Component 2)
        context_transition = visual_context.get('context_transition')
        context_transition_hint = self._get_context_transition_hint(context_transition)

        prompt = f"""You are a Principal BI Engineer specializing in Tableau-to-Power BI migrations.

Your task: Convert the following Tableau calculation to optimized Power BI DAX.

## TABLEAU CALCULATION

**Name:** {calc_name}
**Type:** {calc_type}
**Formula:**
```tableau
{tableau_formula}
```

## VISUAL CONTEXT (CRITICAL FOR DAX GENERATION)

{visual_hint}

**Used in worksheets:** {', '.join(visual_context.get('used_in_worksheets', [])) or 'Unknown'}
**Visual types:** {', '.join(visual_types) or 'Unknown'}
**Grouping dimensions (partition_by):** {', '.join(visual_context.get('partition_by', [])) or 'None'}
**Sort order:** {', '.join(visual_context.get('sort_by', [])) or 'None'}
**Applied filters:** {', '.join(visual_context.get('filters', [])) or 'None'}

## DATA PROFILE

**Table name:** {table_name}
**Row count:** {data_profile.get('row_count', 'Unknown')}
**Columns:** {data_profile.get('column_count', 'Unknown')}

## CONVERSION PATTERNS LIBRARY

Below are ALL known conversion patterns. Use these as reference when generating DAX.

{patterns_json}

## CONTEXT TRANSITION GUIDANCE (Component 2: Order of Operations)

{context_transition_hint}

## YOUR TASK

1. **Analyze Tableau's Order of Operations:**
   - Context filters apply first
   - FIXED LOD ignores standard filters (use ALLEXCEPT)
   - EXCLUDE LOD removes dimensions (use ALL on excluded dims)
   - INCLUDE LOD adds dimensions (no direct equivalent, use SUMMARIZE)
   - Table calculations run post-aggregation

2. **Find Matching Pattern:**
   - Review ALL patterns above
   - Identify the closest match
   - Note any differences from the pattern

3. **Handle Critical Aspects:**
   - **Null Handling:** Tableau ZN() vs DAX DIVIDE() with default
   - **Filter Context:** Use CALCULATE, ALL, ALLEXCEPT appropriately
   - **Aggregation:** Ensure proper measure vs calculated column
   - **LOD Expressions:** Map FIXED/INCLUDE/EXCLUDE correctly
   - **Visual Context:** Adjust DAX based on where calculation is used

4. **Generate Optimized DAX:**
   - Use proper DAX syntax
   - Include measure name (e.g., "Measure Name = ...")
   - Use DIVIDE() instead of / for safety
   - Apply best practices (ALLEXCEPT over multiple ALL)

5. **Provide Reasoning:**
   - Explain your approach
   - Note which pattern you used (or why you deviated)
   - Identify any potential issues

6. **Assign Confidence Score:**
   - 1.0 = Exact pattern match, high confidence
   - 0.9 = Close match with minor adjustments
   - 0.8 = Good conversion, some complexity
   - 0.7 = Complex case, may need validation
   - <0.7 = Low confidence, manual review recommended

## OUTPUT FORMAT

Return ONLY valid JSON (no markdown code blocks):

{{
  "dax_formula": "Measure Name = DAX formula here",
  "reasoning": "Detailed explanation of your approach...",
  "confidence": 0.95,
  "pattern_used": "pattern_id or null",
  "warnings": ["Warning 1 if any", "Warning 2 if any"]
}}

## IMPORTANT RULES

- ALWAYS use DIVIDE() instead of / to avoid division by zero
- ALWAYS specify table name: Table[Column]
- For FIXED LOD, use CALCULATE with ALLEXCEPT
- For aggregations, create measures (not calculated columns)
- If visual type is CARD (single value), ensure DAX returns a scalar
- If visual type is MATRIX, ensure DAX works with grouping context
- For row-level calculations, use calculated columns
- Return ONLY JSON, no other text

Now generate the DAX conversion:
"""

        return prompt

    def _parse_llm_response(self, response: str, original_formula: str) -> DAXResult:
        """
        Parse LLM response JSON

        Args:
            response: Raw LLM response
            original_formula: Original Tableau formula (for fallback)

        Returns:
            DAXResult
        """
        try:
            # Clean response (remove markdown code blocks if present)
            cleaned = response.strip()

            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]

            cleaned = cleaned.strip()

            # Parse JSON
            data = json.loads(cleaned)

            return DAXResult(
                dax_formula=data.get("dax_formula", ""),
                reasoning=data.get("reasoning", ""),
                confidence=float(data.get("confidence", 0.7)),
                method="LLM_PATTERN",
                warnings=data.get("warnings", []),
                pattern_used=data.get("pattern_used")
            )

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            logger.debug(f"Response: {response}")

            # Fallback: extract formula from response text
            dax_match = re.search(r'dax_formula["\s:]+([^"]+)', response)
            if dax_match:
                dax = dax_match.group(1)
            else:
                dax = f"-- PARSE ERROR: {original_formula}"

            return DAXResult(
                dax_formula=dax,
                reasoning="Failed to parse LLM response",
                confidence=0.5,
                method="LLM_GENERATED",
                warnings=["JSON parse error - manual review required"]
            )

    def _fallback_conversion(self, calc_node: CalculationNode, table_name: str) -> DAXResult:
        """
        Fallback conversion when LLM fails

        Uses basic regex-based rules.
        """
        formula = calc_node.formula
        dax = formula

        # Basic substitutions
        # [FieldName] -> Table[FieldName]
        dax = re.sub(r'\[([^\]]+)\]', f'{table_name}[\\1]', dax)

        # SUM(...) stays the same
        # / -> DIVIDE
        dax = re.sub(r'(\S+)\s*/\s*(\S+)', r'DIVIDE(\1, \2, 0)', dax)

        dax_formula = f"{calc_node.name} = {dax}"

        return DAXResult(
            dax_formula=dax_formula,
            reasoning="LLM failed, using basic rule-based conversion",
            confidence=0.5,
            method="RULE_BASED",
            warnings=["LLM conversion failed - manual review required"]
        )

    def _get_visual_context_hint(self, visual_types: List[str], visual_context: Dict[str, Any]) -> str:
        """
        Generate human-readable hint about visual context for LLM

        This helps the LLM understand WHERE the calculation is used,
        which is critical for generating correct DAX.
        """
        if not visual_types:
            return "**Context:** Unknown - generate a general-purpose DAX measure."

        # Get primary visual type (most common)
        primary_visual = visual_types[0] if visual_types else "unknown"
        partition_by = visual_context.get('partition_by', [])

        # Generate specific guidance based on visual type
        if primary_visual == "card":
            return """**Context:** This calculation is displayed as a SINGLE VALUE (Card visual).
→ The DAX MUST return a scalar value (single number).
→ Use aggregate functions like SUM(), AVG(), COUNT().
→ Do NOT use SUMX() or row-level iterations unless necessary."""

        elif primary_visual == "matrix":
            return f"""**Context:** This calculation is in a MATRIX/CROSSTAB (Pivot Table).
→ The DAX will be evaluated in multiple grouping contexts.
→ Grouping dimensions: {', '.join(partition_by) if partition_by else 'Dynamic based on visual'}.
→ Ensure DAX works correctly when sliced by different dimensions."""

        elif primary_visual in ["bar", "line", "area"]:
            return f"""**Context:** This calculation is in a {primary_visual.upper()} CHART.
→ The DAX will be evaluated for each point on the chart.
→ Partition: {', '.join(partition_by) if partition_by else 'Varies by chart axis'}.
→ Use measure (not calculated column) for best performance."""

        elif primary_visual == "text_table":
            return """**Context:** This calculation is in a TEXT TABLE (simple table).
→ DAX will be evaluated row-by-row in the table context.
→ Ensure proper aggregation based on table grouping."""

        elif primary_visual == "scatter":
            return """**Context:** This calculation is in a SCATTER PLOT.
→ Each point represents an aggregated value.
→ Ensure DAX aggregates correctly for x/y coordinates."""

        else:
            return f"""**Context:** This calculation is used in: {', '.join(visual_types)}.
→ Generate a flexible DAX measure that works in multiple contexts."""

    def _get_context_transition_hint(self, context_transition) -> str:
        """
        Generate LLM guidance for context transitions (Component 2)

        This tells the LLM HOW the evaluation context changes and
        which DAX pattern to use (ALLEXCEPT, ALL, KEEPFILTERS, etc.)
        """
        if not context_transition:
            return "**No context transition** - Standard aggregation. Use basic DAX measure."

        trans = context_transition
        trans_type = trans.get('transition_type', 'NONE')

        if trans_type == 'FIXED_LOD':
            return f"""**🔄 CONTEXT TRANSITION: FIXED LOD**

{trans.get('explanation', '')}

**From:** {trans.get('from_context', 'View context')}
**To:** {trans.get('to_context', 'Fixed context')}

**DAX Pattern:** {trans.get('dax_pattern', 'CALCULATE with ALLEXCEPT')}

**CRITICAL:**
- Use ALLEXCEPT to keep only the FIXED dimensions
- FIXED ignores view filters except context filters
- Example: `CALCULATE(SUM(Sales[Amount]), ALLEXCEPT(Sales, Sales[Region]))`
"""

        elif trans_type == 'EXCLUDE_LOD':
            return f"""**🔄 CONTEXT TRANSITION: EXCLUDE LOD**

{trans.get('explanation', '')}

**From:** {trans.get('from_context', 'View context')}
**To:** {trans.get('to_context', 'Excluding dimension')}

**DAX Pattern:** {trans.get('dax_pattern', 'CALCULATE with ALL')}

**CRITICAL:**
- Use ALL() on the excluded dimension
- Removes that dimension from grouping
- Example: `CALCULATE(SUM(Sales[Amount]), ALL(Sales[ExcludedDim]))`
"""

        elif trans_type == 'INCLUDE_LOD':
            return f"""**🔄 CONTEXT TRANSITION: INCLUDE LOD**

{trans.get('explanation', '')}

**From:** {trans.get('from_context', 'View context')}
**To:** {trans.get('to_context', 'With added dimension')}

**DAX Pattern:** {trans.get('dax_pattern', 'SUMMARIZE or calculated table')}

**CRITICAL:**
- INCLUDE has NO direct DAX equivalent
- Options:
  1. Add dimension to visual
  2. Use SUMMARIZE for calculated table
  3. Redesign calculation logic
"""

        elif trans_type == 'CONTEXT_FILTER':
            return f"""**🔄 CONTEXT TRANSITION: CONTEXT FILTER**

{trans.get('explanation', '')}

**DAX Pattern:** {trans.get('dax_pattern', 'KEEPFILTERS')}

**CRITICAL:**
- Context filters apply BEFORE standard filters
- Use KEEPFILTERS to preserve filter order
- Example: `CALCULATE(expr, KEEPFILTERS(Sales[Year] = 2024))`
"""

        elif trans_type == 'TABLE_CALC':
            return f"""**🔄 CONTEXT TRANSITION: TABLE CALCULATION**

{trans.get('explanation', '')}

**Note:** Table calculations often require Power BI model changes (Index columns, Date tables, etc.)
See model enhancement guidance.
"""

        else:
            return "**No special context transition** - Standard measure."

    def _context_transition_to_dict(self, transition) -> Dict[str, Any]:
        """
        Convert ContextTransition dataclass to dictionary for JSON serialization
        """
        if not transition:
            return None

        return {
            "transition_type": transition.transition_type.value if hasattr(transition.transition_type, 'value') else str(transition.transition_type),
            "from_context": transition.from_context,
            "to_context": transition.to_context,
            "dax_pattern": transition.dax_pattern,
            "explanation": transition.explanation,
            "requires_allexcept": transition.requires_allexcept,
            "requires_all": transition.requires_all,
            "requires_keepfilters": transition.requires_keepfilters
        }

    # ============================================
    # Specialized Conversion Methods
    # ============================================

    def convert_lod_expression(
        self,
        lod_type: str,
        dimensions: List[str],
        aggregation_formula: str,
        table_name: str
    ) -> DAXResult:
        """
        Convert LOD expression to DAX

        Args:
            lod_type: FIXED, INCLUDE, EXCLUDE
            dimensions: List of dimension fields
            aggregation_formula: The aggregation part (e.g., "SUM([Sales])")
            table_name: Table name

        Returns:
            DAXResult with LOD conversion
        """
        logger.info(f"Converting LOD expression: {lod_type} on {dimensions}")

        if lod_type == "FIXED":
            # FIXED: Use CALCULATE with ALLEXCEPT
            if dimensions:
                dim_refs = [f"{table_name}[{dim}]" for dim in dimensions]
                allexcept = f"ALLEXCEPT({table_name}, {', '.join(dim_refs)})"

                dax = f"""CALCULATE(
    {aggregation_formula},
    {allexcept}
)"""
            else:
                # FIXED with no dimensions = grand total
                dax = f"""CALCULATE(
    {aggregation_formula},
    ALL({table_name})
)"""

            return DAXResult(
                dax_formula=dax,
                reasoning=f"FIXED LOD converted using ALLEXCEPT for dimensions: {dimensions}",
                confidence=0.90,
                method="RULE_BASED",
                warnings=[],
                pattern_used="fixed_lod"
            )

        elif lod_type == "EXCLUDE":
            # EXCLUDE: Use CALCULATE with ALL(column)
            all_dims = [f"ALL({table_name}[{dim}])" for dim in dimensions]

            dax = f"""CALCULATE(
    {aggregation_formula},
    {', '.join(all_dims)}
)"""

            return DAXResult(
                dax_formula=dax,
                reasoning=f"EXCLUDE LOD converted using ALL() for excluded dimensions: {dimensions}",
                confidence=0.85,
                method="RULE_BASED",
                warnings=[],
                pattern_used="exclude_lod"
            )

        elif lod_type == "INCLUDE":
            # INCLUDE is tricky - no direct equivalent
            return DAXResult(
                dax_formula=f"-- INCLUDE LOD: {aggregation_formula}",
                reasoning="INCLUDE LOD has no direct DAX equivalent. Consider adding dimension to visual or using SUMMARIZE.",
                confidence=0.60,
                method="RULE_BASED",
                warnings=[
                    "INCLUDE LOD requires different approach in Power BI",
                    "May need to add dimension to visual or redesign calculation"
                ],
                pattern_used="include_lod"
            )

        else:
            return self._fallback_conversion(
                CalculationNode(
                    calc_id="unknown",
                    name="LOD Expression",
                    formula=f"{{{lod_type} {dimensions}: {aggregation_formula}}}",
                    calc_type=None,
                    granularity=None,
                    depends_on=[],
                    dependency_level=0,
                    visual_context=None
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
        """
        Convert Tableau parameter to Power BI disconnected table

        Args:
            parameter_name: Parameter name
            datatype: Data type
            allowable_values: List of allowed values
            table_name: Disconnected table name

        Returns:
            DAXResult with parameter instructions
        """
        # Generate disconnected table instructions
        values_str = ", ".join([f'"{v}"' if isinstance(v, str) else str(v) for v in allowable_values])

        dax = f"""{parameter_name} =
SELECTEDVALUE(
    '{table_name}'[{parameter_name}],
    "{allowable_values[0] if allowable_values else 'Default'}"
)"""

        instructions = f"""
-- PARAMETER CONVERSION INSTRUCTIONS --

1. Create disconnected table in Power Query:
   Table name: {table_name}
   Column: {parameter_name}
   Values: {values_str}

2. Add slicer to report using {table_name}[{parameter_name}]

3. Use this measure to get selected value:
{dax}
"""

        return DAXResult(
            dax_formula=dax,
            reasoning=instructions,
            confidence=0.85,
            method="RULE_BASED",
            warnings=["Requires creating disconnected parameter table in Power Query"],
            pattern_used="parameter_single_value"
        )
