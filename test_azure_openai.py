# Tableau to DAX Conversion Pattern Library
# Version: 1.0
# Description: Curated patterns for LLM-powered Tableau-to-DAX conversion

version: "1.0"
description: "Production-grade conversion patterns for Tableau formulas to Power BI DAX"

patterns:
  # ============================================
  # Basic Aggregations
  # ============================================

  - pattern_id: simple_sum
    tableau: "SUM([Sales])"
    dax: "Total Sales = SUM(Sales[Sales])"
    confidence: 1.0
    tags: [basic, aggregation, sum]
    notes: "Direct aggregation - no context needed. Always use SUM(Table[Column]) syntax."
    context:
      granularity: "aggregate"
      filter_context: "inherits from visual"

  - pattern_id: simple_avg
    tableau: "AVG([Price])"
    dax: "Average Price = AVERAGE(Products[Price])"
    confidence: 1.0
    tags: [basic, aggregation, average]
    notes: "Simple average. Use AVERAGE, not AVG in DAX."
    context:
      granularity: "aggregate"

  - pattern_id: simple_count
    tableau: "COUNT([Order ID])"
    dax: "Order Count = COUNT(Orders[Order ID])"
    confidence: 1.0
    tags: [basic, aggregation, count]
    notes: "Counts non-blank values."
    context:
      granularity: "aggregate"

  - pattern_id: countd
    tableau: "COUNTD([Customer ID])"
    dax: "Unique Customers = DISTINCTCOUNT(Sales[Customer ID])"
    confidence: 1.0
    tags: [basic, aggregation, distinct]
    notes: "COUNTD in Tableau = DISTINCTCOUNT in DAX."
    context:
      granularity: "aggregate"

  - pattern_id: simple_min
    tableau: "MIN([Date])"
    dax: "First Date = MIN(Sales[Date])"
    confidence: 1.0
    tags: [basic, aggregation, min]
    notes: "Works for numeric and date columns."
    context:
      granularity: "aggregate"

  - pattern_id: simple_max
    tableau: "MAX([Date])"
    dax: "Last Date = MAX(Sales[Date])"
    confidence: 1.0
    tags: [basic, aggregation, max]
    notes: "Works for numeric and date columns."
    context:
      granularity: "aggregate"

  # ============================================
  # Ratios and Division
  # ============================================

  - pattern_id: ratio
    tableau: "SUM([Profit]) / SUM([Sales])"
    dax: "Profit Ratio = DIVIDE(SUM(Sales[Profit]), SUM(Sales[Sales]), 0)"
    confidence: 0.95
    tags: [ratio, division, divide]
    notes: |
      Use DIVIDE to avoid division by zero errors.
      Third parameter (0) is the default value when denominator is zero.
      Tableau returns NULL on division by zero; DAX can return custom value.
    context:
      granularity: "aggregate"
      null_handling: "DIVIDE handles nulls gracefully"

  - pattern_id: percent_of_total
    tableau: "SUM([Sales]) / TOTAL(SUM([Sales]))"
    dax: |
      % of Total Sales =
      DIVIDE(
          SUM(Sales[Sales]),
          CALCULATE(SUM(Sales[Sales]), ALL(Sales)),
          0
      )
    confidence: 0.90
    tags: [ratio, percent, total, all]
    notes: |
      TOTAL in Tableau removes all dimensions.
      Use ALL() in DAX to remove filter context.
      Can also use ALLEXCEPT to keep specific dimensions.
    context:
      granularity: "aggregate"
      filter_context: "ALL removes filters"

  # ============================================
  # Conditional Logic
  # ============================================

  - pattern_id: if_then_else
    tableau: "IF [Sales] > 1000 THEN 'High' ELSE 'Low' END"
    dax: "Sales Category = IF(Sales[Sales] > 1000, \"High\", \"Low\")"
    confidence: 0.95
    tags: [conditional, if, logic]
    notes: |
      Tableau: IF...THEN...ELSE...END
      DAX: IF(condition, true_value, false_value)
      Use double quotes for strings in DAX.
    context:
      granularity: "row_level"

  - pattern_id: case_when
    tableau: |
      CASE [Region]
      WHEN 'East' THEN 1
      WHEN 'West' THEN 2
      ELSE 3
      END
    dax: |
      Region Code =
      SWITCH(
          Sales[Region],
          "East", 1,
          "West", 2,
          3
      )
    confidence: 0.95
    tags: [conditional, case, switch]
    notes: |
      Tableau CASE = DAX SWITCH.
      SWITCH is more efficient than nested IFs.
      Last value is the default (ELSE).
    context:
      granularity: "row_level"

  # ============================================
  # LOD Expressions (CRITICAL)
  # ============================================

  - pattern_id: fixed_lod
    tableau: "{FIXED [Region]: SUM([Sales])}"
    dax: |
      Sales by Region =
      CALCULATE(
          SUM(Sales[Sales]),
          ALLEXCEPT(Sales, Sales[Region])
      )
    confidence: 0.90
    tags: [lod, fixed, calculate, allexcept]
    notes: |
      FIXED LOD ignores all filters EXCEPT the specified dimensions.
      Use CALCULATE with ALLEXCEPT to achieve same behavior.
      CRITICAL: FIXED ignores view filters but respects context filters.
    context:
      granularity: "aggregate"
      filter_context: "ALLEXCEPT removes all filters except specified columns"
      evaluation_order: "context filters -> FIXED -> standard filters"

  - pattern_id: fixed_lod_no_dims
    tableau: "{FIXED: SUM([Sales])}"
    dax: |
      Total Sales (All) =
      CALCULATE(
          SUM(Sales[Sales]),
          ALL(Sales)
      )
    confidence: 0.95
    tags: [lod, fixed, all]
    notes: |
      FIXED without dimensions = grand total.
      Use ALL(Table) to remove all filters.
    context:
      granularity: "aggregate"
      filter_context: "ALL removes all filters"

  - pattern_id: include_lod
    tableau: "{INCLUDE [Category]: SUM([Sales])}"
    dax: |
      -- INCLUDE LOD requires different approach
      -- Add Category to visual or use SUMMARIZE
      Sales with Category =
      CALCULATE(
          SUM(Sales[Sales])
          -- Category context is added by visual
      )
    confidence: 0.70
    tags: [lod, include, complex]
    notes: |
      INCLUDE LOD is tricky - it ADDS dimensions to the view.
      No direct DAX equivalent. Options:
      1. Add the dimension to the visual
      2. Use SUMMARIZE to create virtual table
      3. Reconsider the calculation approach
    context:
      granularity: "aggregate"
      warning: "May require model changes or different approach"

  - pattern_id: exclude_lod
    tableau: "{EXCLUDE [Month]: SUM([Sales])}"
    dax: |
      Sales Excluding Month =
      CALCULATE(
          SUM(Sales[Sales]),
          ALL(Sales[Month])
      )
    confidence: 0.85
    tags: [lod, exclude, all]
    notes: |
      EXCLUDE LOD removes specified dimensions from context.
      Use ALL(Column) to remove that dimension's filter.
      Can use multiple ALL() for multiple dimensions.
    context:
      granularity: "aggregate"
      filter_context: "ALL(Column) removes that dimension"

  # ============================================
  # Null Handling
  # ============================================

  - pattern_id: zn_function
    tableau: "ZN([Profit])"
    dax: "Profit (No Nulls) = IF(ISBLANK(Sales[Profit]), 0, Sales[Profit])"
    confidence: 0.95
    tags: [null, zn, blank]
    notes: |
      ZN (Zero if Null) in Tableau = custom IF in DAX.
      Alternative: Use DIVIDE with default value.
    context:
      granularity: "row_level"

  - pattern_id: isnull_function
    tableau: "ISNULL([Discount])"
    dax: "Is Discount Null = ISBLANK(Sales[Discount])"
    confidence: 1.0
    tags: [null, isnull, blank]
    notes: "ISNULL in Tableau = ISBLANK in DAX."
    context:
      granularity: "row_level"

  # ============================================
  # Date Functions
  # ============================================

  - pattern_id: year_function
    tableau: "YEAR([Order Date])"
    dax: "Order Year = YEAR(Sales[Order Date])"
    confidence: 1.0
    tags: [date, year, time]
    notes: "Date functions have same name in Tableau and DAX."
    context:
      granularity: "row_level"

  - pattern_id: datediff
    tableau: "DATEDIFF('day', [Start Date], [End Date])"
    dax: "Days Difference = DATEDIFF(Sales[Start Date], Sales[End Date], DAY)"
    confidence: 0.95
    tags: [date, datediff, time]
    notes: |
      Tableau: DATEDIFF('interval', start, end)
      DAX: DATEDIFF(start, end, interval)
      Interval constants: DAY, MONTH, YEAR, QUARTER
    context:
      granularity: "row_level"

  # ============================================
  # String Functions
  # ============================================

  - pattern_id: concatenation
    tableau: "[First Name] + ' ' + [Last Name]"
    dax: "Full Name = Customers[First Name] & \" \" & Customers[Last Name]"
    confidence: 1.0
    tags: [string, concatenation, text]
    notes: |
      Tableau uses + for concatenation.
      DAX uses & (ampersand).
    context:
      granularity: "row_level"

  - pattern_id: upper_lower
    tableau: "UPPER([Product Name])"
    dax: "Product Name Upper = UPPER(Products[Product Name])"
    confidence: 1.0
    tags: [string, upper, text]
    notes: "String functions have same names."
    context:
      granularity: "row_level"

  # ============================================
  # Parameters
  # ============================================

  - pattern_id: parameter_single_value
    tableau: "[Date Granularity Parameter]"
    dax: |
      -- Create disconnected parameter table
      Date Granularity = SELECTEDVALUE('Date Granularity'[Value], "Month")
    confidence: 0.85
    tags: [parameter, selectedvalue]
    notes: |
      Tableau parameters = disconnected tables in Power BI.
      Steps:
      1. Create table: Date Granularity with column: Value
      2. Add values: Year, Quarter, Month, Week, Day
      3. Add slicer to report
      4. Use SELECTEDVALUE to get current selection
    context:
      granularity: "scalar"
      warning: "Requires creating disconnected table"

  # ============================================
  # Table Calculations (ADVANCED)
  # ============================================

  - pattern_id: rank
    tableau: "RANK(SUM([Sales]))"
    dax: |
      Sales Rank =
      RANKX(
          ALL(Products[Product Name]),
          SUM(Sales[Sales]),
          ,
          DESC,
          Dense
      )
    confidence: 0.80
    tags: [table_calc, rank, rankx]
    notes: |
      Tableau RANK is a table calculation.
      DAX RANKX requires explicit partition context.
      Use ALL() or ALLEXCEPT() to define ranking scope.
    context:
      granularity: "table"
      warning: "Requires understanding of visual context"

  - pattern_id: running_sum
    tableau: "RUNNING_SUM(SUM([Sales]))"
    dax: |
      Running Total Sales =
      CALCULATE(
          SUM(Sales[Sales]),
          FILTER(
              ALL(Sales[Date]),
              Sales[Date] <= MAX(Sales[Date])
          )
      )
    confidence: 0.75
    tags: [table_calc, running_sum, cumulative]
    notes: |
      Tableau running totals are simpler.
      DAX requires explicit date filtering.
      Works best with continuous date dimension.
    context:
      granularity: "table"
      warning: "Assumes sorted date context"

  # ============================================
  # Window Functions
  # ============================================

  - pattern_id: window_avg
    tableau: "WINDOW_AVG(SUM([Sales]))"
    dax: |
      Window Average =
      AVERAGEX(
          ALL(Sales[Region]),
          SUM(Sales[Sales])
      )
    confidence: 0.70
    tags: [table_calc, window, averagex]
    notes: |
      Window calculations depend heavily on visual layout.
      May need to be recalculated based on Power BI context.
    context:
      granularity: "table"
      warning: "Highly context-dependent"

# ============================================
# Metadata
# ============================================

metadata:
  total_patterns: 26
  coverage:
    basic_aggregations: 6
    ratios: 2
    conditional_logic: 2
    lod_expressions: 4
    null_handling: 2
    date_functions: 2
    string_functions: 2
    parameters: 1
    table_calculations: 3
    window_functions: 1

  difficulty_levels:
    easy: [simple_sum, simple_avg, simple_count, countd, simple_min, simple_max, if_then_else, year_function, upper_lower, concatenation, isnull_function]
    medium: [ratio, percent_of_total, case_when, zn_function, datediff, fixed_lod_no_dims, exclude_lod]
    hard: [fixed_lod, include_lod, parameter_single_value, rank, running_sum, window_avg]

  notes: |
    This pattern library is designed for direct LLM prompting.
    All patterns are passed to the LLM in a single prompt.
    The LLM selects the most appropriate pattern based on:
    1. Tableau formula structure
    2. Visual context (partition, filters)
    3. Data profile (cardinality, null density)

    For best results:
    - Always include visual context in the prompt
    - Provide sample data for ambiguous cases
    - Use self-correction loop for failed validations
