Migration Orchestrator Deep Dive: Tableau to Power BI
This document provides a comprehensive, code-level explanation of the orchestrator.execute_migration process. It details the 7-phase pipeline that transforms a Tableau workbook into a Power BI solution.

1. Overview: The 7-Phase Pipeline
The migration is orchestrated by 
MigrationOrchestrator
 in 
src/tableau/migration_orchestrator.py
. It executes the following phases sequentially:

Parsing: Extract metadata from .twbx files.
Data Profiling: Analyze .hyper extracts for ground truth and statistics.
Logic Graph Construction: Build a dependency DAG to determine execution order.
DAX Generation: AI-powered conversion of Tableau formulas to DAX.
Validation: High-fidelity comparison of Tableau vs. DAX results.
Model Building: Construct the Power BI semantic model (relationships, tables).
PBIX Injection: Programmatically apply changes to a PBIX file.
Phase 1: Parsing (
TableauTWBParser
)
Goal: Extract all structural and logical metadata from the Tableau workbook.

Technical Implementation:

File Handling: Unzips .twbx files to memory. Extracts .twb (XML) and .hyper (Data) files.
XML Parsing: Uses lxml to traverse the Tableau XML tree.
Critical Metadata Extracted:
Calculated Fields: Extracts formula, role (measure/dimension), and data type.
LOD Expressions: Identifies FIXED, INCLUDE, EXCLUDE patterns using Regex (r'\{(FIXED|INCLUDE|EXCLUDE)...').
Visual Context: Parses worksheets to determine how calculations are used (Rows, Columns, Marks). This is critical for DAX generation (e.g., distinguishing a scalar Card visual from a Matrix).
Filters: specifically identifies Context Filters, which require KEEPFILTERS or specific CALCULATE patterns in DAX.
Key Insight: The parser goes beyond simple formula extraction; it builds a "Visual Context" map (used_in_worksheets, visual_types) that informs the AI on how to construct the DAX (e.g., whether to return a scalar or a table-dependent value).

Phase 2: Data Profiling (
HyperDataProfiler
)
Goal: Understand the underlying data and establish a "Ground Truth" for validation.

Technical Implementation:

Hyper API / DuckDB: Uses the native Tableau Hyper API (if available) or falls back to DuckDB to read .hyper extracts.
Profiling:
Generates column statistics: null_percent, 
cardinality
, distinct_count.
Identifies Primary Keys: Columns with 100% uniqueness and 0 nulls.
Ground Truth Extraction:
The 
execute_tableau_formula
 method allows the system to run Tableau formulas against the raw data (using Pandas/DuckDB simulation) to get expected values.
Example: Translates SUM([Sales]) / SUM([Profit]) into a Pandas operation to calculate the exact number for validation.
Phase 3: Logic Graph Construction (
LogicGraphBuilder
)
Goal: resolve dependencies and determine the correct order of conversion.

Technical Implementation:

Dependency DAG: Uses networkx.DiGraph to build a Directed Acyclic Graph.
Nodes: Calculations and Base Fields.
Edges: Dependencies (Calc A depends on Calc B).
Topological Sort: Determines the Execution Order. Base fields -> Level 1 Calcs -> Level 2 Calcs. This ensures that when Calc B is converted, Calc A (which it depends on) has already been converted.
Granularity Detection:
Analyzes formulas to classify them as ROW_LEVEL (Calculated Column) or AGGREGATE (Measure).
Self-Correction: A "Refinement Pass" upgrades CALCULATED_COLUMN to MEASURE if it depends on another Measure, preventing incorrect aggregation handling.
Context Transition Analysis (Component 2):
Analyzes how the evaluation context changes from Tableau to DAX.
FIXED LOD: Maps to CALCULATE(..., ALLEXCEPT(...)) or ALL(...).
Context Filters: Maps to CALCULATE(..., KEEPFILTERS(...)).
Visual Context: Metadata from Phase 1 is attached to nodes to guide DAX strategies (e.g., "This is used in a Matrix, so preserve grouping").
Phase 4: DAX Generation (
DAXGenerator
)
Goal: Convert Tableau formulas to optimized, production-ready DAX.

Technical Implementation:

Exact Pattern Match:
Checks a strict patterns.yaml library for known formulas (e.g., specific KPI patterns).
If confidence > 99%, uses the pre-approved DAX immediately.
LLM Generation:
Prompt Engineering: Constructs a rich prompt containing:
Tableau Formula: The code to convert.
Visual Context: "Used in a Matrix visual grouped by [Region]".
Data Profile: "Column [Sales] has 0 nulls".
Context Transition: "This is a FIXED LOD, so use ALLEXCEPT (from Component 2)".
Field Reference Guide: XML block telling the LLM how to handle each dependency (e.g., <dax_usage>[Net Profit] (DO NOT WRAP)</dax_usage>).
Output: JSON object with 
dax_formula
, reasoning, and confidence.
Key Insight: The generator doesn't just ask "Convert this". It provides a "Field Reference Guide" based on the Logic Graph, explicitly telling the LLM which fields are Measures (don't wrap in SUM) and which are Columns (must wrap in SUM).

Phase 5: High-Fidelity Validation (
ValidationEngine
)
Goal: Prove functional equivalence between Tableau and DAX.

Technical Implementation:

Dual Execution Engine:
Tableau Side: Executes the original formula against the Hyper extract (simulated via Pandas/Evaluator) to get the "Truth Value".
DAX Side: Uses DuckDB to execute the generated DAX formula against the same data (mocking Power BI engine).
Slice-Based Testing:
Generates "Test Slices" (combinations of dimension filters, e.g., Region='East', Year=2023).
Compares results for each slice.
Error Categorization:
PERFECT_MATCH: Delta < 1e-10.
ROUNDING_ERROR: Relative error < 0.01%.
CONTEXT_SHIFT: Large error (>10%) usually indicates a failed LOD or filter context translation.
Self-Correction Loop:
If validation fails, the engine feeds the error (e.g., "Value mismatch for Region=East") back to the LLM.
The LLM generates a refined formula, which is re-validated.
Phase 6 & 7: Model Build & Injection (
PBIXInjector
)
Goal: Create the physical Power BI artifacts.

Technical Implementation:

Tabular Editor Scripting:
The 
PBIXInjector
 generates C# scripts for Tabular Editor.
These scripts utilize the TOM (Tabular Object Model) to programmatically:
Create Tables and Columns.
Create Measures with properties (Display Folder, Format String).
Create Relationships (detecting cardinality from Phase 2 profiles).
Injection:
Runs TabularEditor.exe via subprocess to execute the C# script against a target PBIX file.
Can create a PBIX "from scratch" (creating a blank model) or inject into an existing template.
Technical Constraints & Future Improvements
Profiling Scope: Currently profiles only the first table in a Hyper file. Needs expansion for multi-table schemas.
Date Tables: Creation is currently manual or template-based; programmatic generation of full Date dimension via DAX/M is a planned enhancement.
Complex LODs: Deeply nested LODs (FIXED inside INCLUDE) may require iterative decomposition logic rather than single-pass translation.