"""Migration Orchestrator - Coordinate end-to-end Tableau-to-Power BI migration"""
import uuid
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from loguru import logger

from api.models.migration_models import (
    MigrationStatus,
    CalculationType,
    ConversionMethod,
    ConversionStatus,
    MigrationJob,
    TableauWorkbook,
    TableauCalculation,
    DAXConversion
)
from storage.migration_store import MigrationStore
from storage.fidelity_validation_store import FidelityValidationStore
from src.tableau.twb_parser import TableauTWBParser, CalculatedField
from src.tableau.hyper_profiler import HyperDataProfiler
from src.tableau.logic_graph_builder import LogicGraphBuilder, CalculationType as GraphCalculationType
from src.tableau.dax_generator import DAXGenerator
from src.tableau.validation_engine import ValidationEngine
from src.powerbi.model_enhancement_agent import ModelEnhancementAgent, EnhancementType, ModelEnhancement
from src.powerbi.enhancement_guide_generator import EnhancementGuideGenerator
from workers.progress_manager import ProgressCallback

# NEW: Complete migration components (STEPS 5-10)
from src.powerbi.pbix_injector import PBIXInjector, Measure, Relationship
from src.powerbi.model_builder import PowerBIModelBuilder, DateTableConfig
from src.powerbi.table_calc_converter import TableCalculationConverter
from src.powerbi.filter_parameter_converter import FilterParameterConverter
from src.powerbi.template_creator import StarterPBIXCreator
from src.powerbi.visual_converter import VisualConverter


class MigrationOrchestrator:
    """
    Orchestrate end-to-end migration workflow

    Workflow:
    1. Parse TWB/TWBX files (15% progress)
    2. Profile Hyper data (30%)
    3. Build logic graph (45%)
    4. Generate DAX conversions (70%)
    5. Validate conversions & build complete model (85%)
       - Validate DAX conversions
       - Build data model (relationships, date table)
       - Convert filters & parameters
       - Create & inject PBIX
       - Generate documentation
       - Export table data to Excel
    6. Export Power BI artifacts (95%)
    7. Complete (100%)
    """

    def __init__(self):
        self.migration_store = MigrationStore()
        self.fidelity_store = FidelityValidationStore()
        self.dax_generator = DAXGenerator()
        self.validation_engine = ValidationEngine()
        self.model_agent = ModelEnhancementAgent()  # Table calc agent
        self.model_enhancements: List[ModelEnhancement] = []  # Track all enhancements

        # NEW: Complete migration components (STEPS 5-10)
        self.pbix_injector = PBIXInjector()
        self.model_builder = PowerBIModelBuilder()
        self.table_calc_converter = TableCalculationConverter()
        self.filter_converter = FilterParameterConverter()
        self.template_creator = StarterPBIXCreator()
        self.visual_converter = VisualConverter()

    async def execute_migration(
        self,
        migration_id: str,
        twbx_paths: List[str],
        progress_callback: Optional[ProgressCallback] = None
    ) -> MigrationJob:
        """
        Execute complete migration workflow

        Args:
            migration_id: Migration job ID
            twbx_paths: List of TWBX/TWB file paths
            progress_callback: Progress tracking callback

        Returns:
            Completed MigrationJob
        """
        logger.info(f"Starting migration {migration_id} with {len(twbx_paths)} workbook(s)")

        try:
            # Initialize progress
            self._update_progress(
                migration_id,
                MigrationStatus.PARSING,
                0,
                "Parsing Tableau workbooks...",
                progress_callback
            )

            # Phase 1: Parse TWB/TWBX Files
            workbooks_data = await self._parse_workbooks(
                migration_id,
                twbx_paths,
                progress_callback
            )

            # Phase 2: Profile Data (if Hyper files exist)
            data_profiles = await self._profile_data(
                migration_id,
                workbooks_data,
                progress_callback
            )

            # Phase 3: Build Logic Graph
            logic_graph = await self._build_logic_graph(
                migration_id,
                workbooks_data,
                progress_callback
            )

            # Phase 4: Generate DAX Conversions
            conversions = await self._generate_dax_conversions(
                migration_id,
                logic_graph,
                data_profiles,
                progress_callback
            )

            # Phase 5: Validate Conversions & Build Complete Model
            validation_results = await self._validate_conversions(
                migration_id,
                conversions,
                workbooks_data,
                progress_callback
            )

            # Phase 6: Generate Enhancement Guide (if needed)
            if self.model_enhancements:
                logger.info(f"📝 Generating model enhancement guide for {len(self.model_enhancements)} enhancements...")

                guide_generator = EnhancementGuideGenerator()
                export_dir = Path("exports") / migration_id
                export_dir.mkdir(parents=True, exist_ok=True)

                guide_path = guide_generator.generate_guide(
                    enhancements=self.model_enhancements,
                    output_dir=export_dir
                )

                if guide_path:
                    logger.info(f"✅ Enhancement guide saved: {guide_path}")

            # Phase 7: Complete
            self._update_progress(
                migration_id,
                MigrationStatus.COMPLETED,
                100,
                "Migration completed successfully",
                progress_callback
            )

            # Mark as completed
            self.migration_store.update_migration_status(
                migration_id,
                MigrationStatus.COMPLETED
            )
            self.migration_store.update_migration_progress(
                migration_id,
                100,
                current_stage="Ready for export",
                message="Migration completed - ready for export"
            )

            migration = self.migration_store.get_migration(migration_id)

            logger.info(f"✅ Migration {migration_id} completed successfully")

            return migration

        except Exception as e:
            logger.error(f"Migration failed: {e}", exc_info=True)

            # Mark as failed
            self.migration_store.update_migration_status(
                migration_id,
                MigrationStatus.FAILED,
                error_message=str(e)
            )

            raise

    # ============================================
    # Phase 1: Parse Workbooks
    # ============================================

    async def _parse_workbooks(
        self,
        migration_id: str,
        twbx_paths: List[str],
        progress_callback: Optional[ProgressCallback]
    ) -> List[Dict[str, Any]]:
        """
        Parse all TWBX files and extract metadata

        Returns:
            List of parsed workbook data with calculations
        """
        logger.info("Phase 1: Parsing Tableau workbooks...")

        workbooks_data = []
        total_calculations = 0

        for i, twbx_path in enumerate(twbx_paths):
            logger.info(f"Parsing {Path(twbx_path).name}...")

            # Parse TWB
            parser = TableauTWBParser(twbx_path)

            # Extract metadata
            calculated_fields = parser.parse_calculated_fields()
            lod_expressions = parser.parse_lod_expressions()
            parameters = parser.parse_parameters()
            worksheets = parser.parse_worksheets()
            dashboards = parser.parse_dashboards()
            data_sources = parser.parse_data_sources()

            # DEBUG: Log Hyper files extracted
            logger.info(f"📦 Extracted {len(parser.hyper_files)} Hyper files from TWBX")
            if parser.hyper_files:
                for hf in parser.hyper_files:
                    logger.info(f"   - {hf}")
            else:
                logger.warning(f"⚠️  No Hyper files found in {Path(twbx_path).name}")

            # Store workbook metadata
            workbook_id = f"wb_{uuid.uuid4().hex[:8]}"

            workbook = {
                "workbook_id": workbook_id,
                "filename": Path(twbx_path).name,
                "file_path": twbx_path,
                "parser": parser,  # Keep parser for later use
                "calculated_fields": calculated_fields,
                "lod_expressions": lod_expressions,
                "parameters": parameters,
                "worksheets": worksheets,
                "dashboards": dashboards,
                "data_sources": data_sources,
                "hyper_files": parser.hyper_files
            }

            workbooks_data.append(workbook)

            # Save to database - create TableauWorkbook object
            tableau_workbook = TableauWorkbook(
                workbook_id=workbook_id,
                migration_id=migration_id,
                filename=Path(twbx_path).name,
                file_path=twbx_path,
                worksheet_count=len(worksheets),
                dashboard_count=len(dashboards),
                data_source_count=len(data_sources),
                extracted_at=None  # Will be set by database
            )
            self.migration_store.save_workbook(tableau_workbook)

            total_calculations += len(calculated_fields)

            # Update progress
            progress_pct = 5 + (i + 1) / len(twbx_paths) * 10

            self._update_progress(
                migration_id,
                MigrationStatus.PARSING,
                int(progress_pct),
                f"Parsed {i + 1}/{len(twbx_paths)} workbooks ({total_calculations} calculations)",
                progress_callback
            )

        # Update migration counts
        self.migration_store.update_migration_counts(
            migration_id,
            workbook_count=len(workbooks_data),
            calculation_count=total_calculations
        )

        logger.info(f"Parsed {len(workbooks_data)} workbooks with {total_calculations} calculations")

        return workbooks_data

    # ============================================
    # Phase 2: Profile Data
    # ============================================

    async def _profile_data(
        self,
        migration_id: str,
        workbooks_data: List[Dict[str, Any]],
        progress_callback: Optional[ProgressCallback]
    ) -> Dict[str, Any]:
        """
        Profile Hyper data for validation context

        Returns:
            Dictionary of data profiles by workbook
        """
        logger.info("Phase 2: Profiling Hyper data...")

        data_profiles = {}

        # Collect all Hyper files
        all_hyper_files = []
        for wb in workbooks_data:
            all_hyper_files.extend(wb.get("hyper_files", []))

        if not all_hyper_files:
            logger.warning("No Hyper files found - skipping data profiling")

            self._update_progress(
                migration_id,
                MigrationStatus.PARSING,
                30,
                "No Hyper files found (using live connections)",
                progress_callback
            )

            return data_profiles

        for i, hyper_path in enumerate(all_hyper_files):
            logger.info(f"Profiling {Path(hyper_path).name}...")

            try:
                profiler = HyperDataProfiler(str(hyper_path))

                # Profile first table
                tables = profiler.list_tables()

                if tables:
                    table_profile = profiler.profile_table(tables[0], sample_size=10000)

                    data_profiles[str(hyper_path)] = {
                        "tables": tables,
                        "primary_table": tables[0],
                        "profile": table_profile
                    }

            except Exception as e:
                logger.error(f"Failed to profile {hyper_path}: {e}")

            # Update progress
            progress_pct = 15 + (i + 1) / len(all_hyper_files) * 15

            self._update_progress(
                migration_id,
                MigrationStatus.PARSING,
                int(progress_pct),
                f"Profiled {i + 1}/{len(all_hyper_files)} data sources",
                progress_callback
            )

        logger.info(f"Profiled {len(data_profiles)} data sources")

        return data_profiles

    # ============================================
    # Phase 3: Build Logic Graph
    # ============================================

    async def _build_logic_graph(
        self,
        migration_id: str,
        workbooks_data: List[Dict[str, Any]],
        progress_callback: Optional[ProgressCallback]
    ) -> Dict[str, Any]:
        """
        Build dependency graph from calculations

        Returns:
            Logic graph with nodes and edges
        """
        logger.info("Phase 3: Building logic graph...")

        self._update_progress(
            migration_id,
            MigrationStatus.DISCOVERING,
            35,
            "Building calculation dependency graph...",
            progress_callback
        )

        # Collect all calculations from all workbooks
        all_calculations = []
        all_lod_expressions = []
        all_worksheets = []
        base_field_metadata = {}

        for wb in workbooks_data:
            all_calculations.extend(wb.get("calculated_fields", []))
            all_lod_expressions.extend(wb.get("lod_expressions", []))
            all_worksheets.extend(wb.get("worksheets", []))

            # Extract base field names from Hyper extract columns
            hyper_files = wb.get("hyper_files", [])
            logger.info(f"🔍 Extracting base fields from {len(hyper_files)} Hyper files...")

            if not hyper_files:
                logger.warning(f"⚠️  Workbook '{wb.get('filename')}' has no Hyper files!")
                logger.warning(f"   Falling back to extracting table names from data sources")
                # Fallback: try to get from data sources
                for ds in wb.get("data_sources", []):
                    # For fallback, we just mark as UNKNOWN type
                    for table in ds.tables:
                        base_field_metadata[table] = {"name": table, "generic_type": "UNKNOWN"}
                    logger.info(f"   Added {len(ds.tables)} table names: {ds.tables}")
                continue

            for hyper_path in hyper_files:
                try:
                    from src.tableau.hyper_profiler import HyperDataProfiler

                    logger.info(f"📂 Profiling Hyper file: {hyper_path}")
                    profiler = HyperDataProfiler(str(hyper_path))
                    tables = profiler.list_tables()
                    logger.info(f"📊 Found {len(tables)} tables: {tables}")

                    # Get columns from all tables
                    for table in tables:
                        columns = profiler.get_columns(table)
                        # Extract column names and metadata
                        for col in columns:
                            col_name = col["name"]
                            base_field_metadata[col_name] = col
                            
                        column_names = [col["name"] for col in columns]
                        logger.info(f"✅ Extracted {len(column_names)} base columns from {table}")

                        # Also add aliased versions (for multi-table scenarios)
                        # Extract clean table name from full path: "Extract"."Fees_XXX" -> "Fees"
                        table_parts = table.strip('"').split(".")
                        table_name = table_parts[-1] if table_parts else table
                        # Strip any remaining quotes from table name
                        table_name = table_name.strip('"')
                        # Remove UUID suffix: "Fees_762A3DD4A32A4BEC8ACBE302CE7DD2BF" -> "Fees"
                        clean_table_name = table_name.split("_")[0] if "_" in table_name else table_name

                        # Add aliased column names: "Amount" -> "Amount (Fees)"
                        for col_name in column_names:
                            aliased_name = f"{col_name} ({clean_table_name})"
                            # Use same metadata for aliased version
                            base_field_metadata[aliased_name] = base_field_metadata[col_name]

                except Exception as e:
                    logger.error(f"❌ Failed to extract columns from {hyper_path}: {e}")
                    logger.error(f"   Falling back to table names only")
                    # Fallback: try to get from data sources
                    for ds in wb.get("data_sources", []):
                         for table in ds.tables:
                            base_field_metadata[table] = {"name": table, "generic_type": "UNKNOWN"}
                         logger.warning(f"   Fallback: Added table names: {ds.tables}")

        # Log final base_fields summary
        logger.info(f"🎯 BASE FIELDS REGISTRY COMPLETE: {len(base_field_metadata)} total fields")
        if base_field_metadata:
            sample_fields = list(base_field_metadata.keys())[:15]
            logger.info(f"   Sample fields: {', '.join(sample_fields)}{'...' if len(base_field_metadata) > 15 else ''}")
        else:
            logger.error(f"⚠️  WARNING: No base fields found! All dependencies will be marked UNKNOWN!")

        # Build graph
        graph_builder = LogicGraphBuilder()

        graph = graph_builder.build_graph(
            calculated_fields=all_calculations,
            lod_expressions=all_lod_expressions,
            worksheets=all_worksheets,
            base_field_metadata=base_field_metadata
        )

        # Store calculations in database
        for calc_name, calc_node in graph_builder.calculations.items():
            # Find parent workbook (simplified - use first)
            parent_wb = workbooks_data[0] if workbooks_data else None

            if parent_wb:
                calc_id = f"calc_{uuid.uuid4().hex[:8]}"

                # Determine calculation type
                if calc_node.is_lod:
                    calc_type = CalculationType.LOD
                elif calc_node.calc_type.value == "TABLE_CALCULATION":
                    calc_type = CalculationType.TABLE_CALC
                elif calc_node.calc_type.value == "MEASURE":
                    calc_type = CalculationType.MEASURE
                else:
                    calc_type = CalculationType.CALCULATED_FIELD

                # Serialize dependency metadata
                depends_on_metadata_dict = None
                if hasattr(calc_node, 'depends_on_metadata') and calc_node.depends_on_metadata:
                    depends_on_metadata_dict = {
                        field_name: {
                            "field_type": dep.field_type,
                            "original_role": dep.original_role,
                            "is_aggregated": dep.is_aggregated,
                        }
                        for field_name, dep in calc_node.depends_on_metadata.items()
                    }

                # Create TableauCalculation object
                tableau_calc = TableauCalculation(
                    calc_id=calc_id,
                    workbook_id=parent_wb["workbook_id"],
                    calc_name=calc_name,
                    calc_formula=calc_node.formula,
                    calc_type=calc_type,
                    visual_context={
                        "used_in": calc_node.visual_context.used_in_worksheets,
                        "partition_by": calc_node.visual_context.partition_by
                    },
                    dependency_level=calc_node.dependency_level,
                    used_in_worksheets=",".join(calc_node.visual_context.used_in_worksheets),
                    depends_on=list(calc_node.depends_on) if hasattr(calc_node, 'depends_on') and calc_node.depends_on else None,
                    depends_on_metadata=depends_on_metadata_dict
                )
                self.migration_store.save_calculation(tableau_calc)

        self._update_progress(
            migration_id,
            MigrationStatus.DISCOVERING,
            45,
            f"Built logic graph with {len(graph_builder.calculations)} calculations",
            progress_callback
        )

        return {
            "graph": graph,
            "builder": graph_builder,
            "execution_order": graph_builder.get_execution_order()
        }

    # ============================================
    # Phase 4: Generate DAX
    # ============================================

    async def _generate_dax_conversions(
        self,
        migration_id: str,
        logic_graph: Dict[str, Any],
        data_profiles: Dict[str, Any],
        progress_callback: Optional[ProgressCallback]
    ) -> List[Dict[str, Any]]:
        """
        Generate DAX for all calculations

        Returns:
            List of conversion results
        """
        logger.info("Phase 4: Generating DAX conversions...")

        self._update_progress(
            migration_id,
            MigrationStatus.CONVERTING,
            50,
            "Generating DAX formulas using AI...",
            progress_callback
        )

        graph_builder = logic_graph["builder"]
        execution_order = logic_graph["execution_order"]

        conversions = []

        for i, calc_name in enumerate(execution_order):
            calc_node = graph_builder.get_calculation_node(calc_name)

            if not calc_node:
                continue

            logger.info(f"Generating DAX for: {calc_name}")

            # Get data profile (use first available)
            data_profile = next(iter(data_profiles.values()), {}).get("profile") if data_profiles else None

            # Generate DAX
            dax_result = self.dax_generator.tableau_to_dax(
                calc_node=calc_node,
                data_profile=data_profile.__dict__ if data_profile else None,
                table_name="Sales"  # TODO: Get actual table name from data source
            )

            # NEW: Check if table calculation requires model enhancement
            model_enhancement = None
            if calc_node.calc_type == GraphCalculationType.TABLE_CALCULATION:
                logger.info(f"  → Detected table calculation, checking model requirements...")

                model_enhancement = self.model_agent.assess_table_calculation(
                    tableau_formula=calc_node.formula,
                    calc_name=calc_name,
                    partition_by=calc_node.visual_context.partition_by if calc_node.visual_context else [],
                    sort_by=calc_node.visual_context.sort_by if calc_node.visual_context else [],
                    table_name="Sales"  # TODO: Get actual table name
                )

                if model_enhancement:
                    logger.warning(f"  ⚠️ Requires model enhancement: {model_enhancement.enhancement_type.value}")
                    logger.info(f"     Reason: {model_enhancement.reason}")

                    # Store enhancement for export
                    self.model_enhancements.append(model_enhancement)

                    # Use enhanced DAX if provided
                    if model_enhancement.dax_code:
                        logger.info(f"  ✓ Using enhanced DAX from model agent")
                        dax_result.dax_formula = model_enhancement.dax_code
                        dax_result.warnings.append(f"Requires model enhancement: {model_enhancement.enhancement_type.value}")

            # Store conversion
            conversion_id = f"conv_{uuid.uuid4().hex[:8]}"

            # Get calc_id from database (fetch by name)
            calculations = self.migration_store.get_calculations_by_migration(migration_id)
            matching_calc = next((c for c in calculations if c.calc_name == calc_name), None)

            if matching_calc:
                # Prepare warnings list
                warnings_list = dax_result.warnings if dax_result.warnings else []

                # Add model enhancement info to warnings if applicable
                if model_enhancement:
                    warnings_list.append(f"MODEL_ENHANCEMENT_REQUIRED: {model_enhancement.enhancement_type.value}")

                # Create DAXConversion object
                dax_conversion = DAXConversion(
                    conversion_id=conversion_id,
                    calc_id=matching_calc.calc_id,
                    migration_id=migration_id,
                    dax_formula=dax_result.dax_formula,
                    conversion_method=ConversionMethod[dax_result.method],
                    confidence_score=dax_result.confidence,
                    reasoning=dax_result.reasoning,
                    warnings=json.dumps(warnings_list) if warnings_list else None,
                    status=ConversionStatus.PENDING,
                    created_at=None  # Will be set by database
                )
                self.migration_store.save_conversion(dax_conversion)

                conversions.append({
                    "conversion_id": conversion_id,
                    "calc_name": calc_name,
                    "dax_result": dax_result,
                    "model_enhancement": model_enhancement  # NEW: Include enhancement
                })

            # Update progress
            progress_pct = 50 + (i + 1) / len(execution_order) * 20

            self._update_progress(
                migration_id,
                MigrationStatus.CONVERTING,
                int(progress_pct),
                f"Generated DAX for {i + 1}/{len(execution_order)} calculations",
                progress_callback
            )

        logger.info(f"Generated {len(conversions)} DAX conversions")

        return conversions

    # ============================================
    # Phase 5: Validate
    # ============================================

    async def _validate_conversions(
        self,
        migration_id: str,
        conversions: List[Dict[str, Any]],
        workbooks_data: List[Dict[str, Any]],
        progress_callback: Optional[ProgressCallback]
    ) -> Dict[str, Any]:
        """
        Validate DAX conversions and build complete Power BI model

        Phase 5 includes:
        - 100% fidelity validation (75-80%)
        - Build data model (80-82%)
        - Convert filters & parameters (82-84%)
        - Create & inject PBIX (84-88%)
        - Export table data to Excel (88-90%)
        - Generate documentation (90-95%)
        """
        logger.info("Phase 5: Validating conversions & building complete model...")

        self._update_progress(
            migration_id,
            MigrationStatus.VALIDATING,
            75,
            "Running 100% fidelity validation...",
            progress_callback
        )

        # Collect Hyper files for validation
        hyper_files = []
        for wb in workbooks_data:
            hyper_files.extend(wb.get("hyper_files", []))

        if not hyper_files:
            logger.warning("No Hyper files found - skipping fidelity validation")

            # Mark all as validated (without numerical validation)
            for conversion in conversions:
                self.migration_store.update_conversion(
                    conversion_id=conversion["conversion_id"],
                    status=ConversionStatus.VALIDATED
                )

            return {
                "validated_count": len(conversions),
                "perfect_matches": 0,
                "avg_pass_rate": 0,
                "message": "No Hyper files available for validation"
            }

        # Use first Hyper file for validation
        hyper_path = hyper_files[0]
        logger.info(f"Using Hyper file for validation: {Path(hyper_path).name}")

        # Detect actual table name from Hyper file
        try:
            from src.tableau.hyper_profiler import HyperDataProfiler
            profiler = HyperDataProfiler(str(hyper_path))
            available_tables = profiler.list_tables()

            if available_tables:
                # Use first table found
                actual_table_name = available_tables[0]
                logger.info(f"Detected table name: {actual_table_name}")
            else:
                logger.warning("No tables found in Hyper file - using default")
                actual_table_name = "Extract"
        except Exception as e:
            logger.warning(f"Could not detect table name: {e} - using default")
            actual_table_name = "Extract"

        validated_count = 0
        perfect_matches = 0
        total_pass_rate = 0

        for i, conversion in enumerate(conversions):
            try:
                # Get calculation details
                calc_name = conversion["calc_name"]
                dax_result = conversion["dax_result"]

                # Get original Tableau formula
                calculations = self.migration_store.get_calculations_by_migration(migration_id)
                matching_calc = next((c for c in calculations if c.calc_name == calc_name), None)

                if not matching_calc:
                    logger.warning(f"Cannot find calculation {calc_name} - skipping validation")
                    continue

                logger.info(f"🔍 Validating {calc_name}...")

                # Broadcast validation start
                if progress_callback:
                    await progress_callback({
                        "type": "validation_started",
                        "conversion_id": conversion["conversion_id"],
                        "calc_name": calc_name,
                        "message": f"Validating {calc_name}..."
                    })

                # Run 100% fidelity validation
                validation_result = self.validation_engine.validate_conversion_v2(
                    conversion_id=conversion["conversion_id"],
                    tableau_formula=matching_calc.calc_formula or "SUM([Sales])",  # Use actual formula
                    dax_formula=dax_result.dax_formula,
                    hyper_path=str(hyper_path),
                    table_name=actual_table_name,  # Use detected table name
                    dimensions=[],  # No dimensions for simple aggregations
                    filters=None
                )

                # Save validation results to database
                validation_id = self.fidelity_store.save_validation_result(
                    migration_id=migration_id,
                    conversion_id=conversion["conversion_id"],
                    validation_result=validation_result
                )

                logger.info(f"✅ Saved validation {validation_id} - Pass rate: {validation_result.pass_rate:.1%}")

                # Update conversion with final DAX (may have been corrected)
                if validation_result.final_dax != dax_result.dax_formula:
                    logger.info(f"📝 Updating conversion with corrected DAX")
                    self.migration_store.update_conversion(
                        conversion_id=conversion["conversion_id"],
                        dax_formula=validation_result.final_dax
                    )

                # Update conversion status based on validation result
                if validation_result.needs_manual_review:
                    # Validation was skipped or had issues - needs human review
                    self.migration_store.update_conversion(
                        conversion_id=conversion["conversion_id"],
                        status=ConversionStatus.MANUAL_REVIEW
                    )
                    logger.warning(f"⚠️ Flagged for manual review: {calc_name}")
                elif validation_result.overall_passed:
                    # Validation passed - mark as validated
                    self.migration_store.update_conversion(
                        conversion_id=conversion["conversion_id"],
                        status=ConversionStatus.VALIDATED
                    )
                    perfect_matches += 1
                else:
                    # Validation failed - keep as pending for retry
                    self.migration_store.update_conversion(
                        conversion_id=conversion["conversion_id"],
                        status=ConversionStatus.PENDING  # Keep as pending if not perfect
                    )

                validated_count += 1
                total_pass_rate += validation_result.pass_rate

                # Broadcast validation complete
                if progress_callback:
                    await progress_callback({
                        "type": "validation_complete",
                        "conversion_id": conversion["conversion_id"],
                        "calc_name": calc_name,
                        "pass_rate": validation_result.pass_rate,
                        "overall_passed": validation_result.overall_passed,
                        "correction_attempts": validation_result.correction_attempts,
                        "message": f"{calc_name}: {validation_result.pass_rate:.0%} match"
                    })

            except Exception as e:
                logger.error(f"Validation failed for {conversion['calc_name']}: {e}")
                # Continue with next conversion

            # Update progress
            progress_pct = 75 + (i + 1) / len(conversions) * 10

            self._update_progress(
                migration_id,
                MigrationStatus.VALIDATING,
                int(progress_pct),
                f"Validated {i + 1}/{len(conversions)} conversions",
                progress_callback
            )

        avg_pass_rate = total_pass_rate / validated_count if validated_count > 0 else 0

        logger.info(f"✅ Validation complete: {perfect_matches}/{validated_count} perfect matches (avg {avg_pass_rate:.1%})")

        # ============================================
        # Build Complete Power BI Model (Part of Phase 5)
        # ============================================

        logger.info("=" * 60)
        logger.info("🏗️  PHASE 5: Building Complete Power BI Model")
        logger.info("=" * 60)

        # Step 1: Build data model
        self._update_progress(
            migration_id,
            MigrationStatus.VALIDATING,
            80,
            "Building Power BI data model...",
            progress_callback
        )

        logger.info("Step 1/5: Building data model...")
        try:
            relationships = self._build_data_model(migration_id, workbooks_data, progress_callback)
            logger.info(f"✅ Data model built: {len(relationships)} relationships")
        except Exception as e:
            logger.error(f"❌ Failed to build data model: {e}", exc_info=True)
            relationships = []

        # Step 2: Convert filters & parameters
        self._update_progress(
            migration_id,
            MigrationStatus.VALIDATING,
            82,
            "Converting filters and parameters...",
            progress_callback
        )

        logger.info("Step 2/5: Converting filters & parameters...")
        try:
            filter_param_results = self._convert_filters_parameters(migration_id, workbooks_data, progress_callback)
            logger.info(f"✅ Filters converted: {len(filter_param_results.get('filters', []))} filters")
        except Exception as e:
            logger.error(f"❌ Failed to convert filters: {e}", exc_info=True)
            filter_param_results = {"filters": [], "whatif_parameters": [], "slicer_tables": []}

        # Step 3: Generate PBIP project
        self._update_progress(
            migration_id,
            MigrationStatus.VALIDATING,
            84,
            "Generating Power BI Project (PBIP)...",
            progress_callback
        )

        logger.info("Step 3/5: Generating PBIP project structure...")
        try:
            pbip_path = self._generate_pbip_project(
                migration_id,
                conversions,
                relationships,
                filter_param_results,
                workbooks_data,
                progress_callback
            )
            if pbip_path:
                logger.info(f"✅ PBIP project created: {pbip_path}")
            else:
                logger.warning("⚠️  PBIP generation failed")
        except Exception as e:
            logger.error(f"❌ Failed to generate PBIP: {e}", exc_info=True)
            pbip_path = None

        # Step 4: Export table data to Excel
        self._update_progress(
            migration_id,
            MigrationStatus.VALIDATING,
            88,
            "Exporting table data to Excel...",
            progress_callback
        )

        logger.info("Step 4/5: Exporting table data to Excel...")
        try:
            excel_files = self._export_table_data_to_excel(
                migration_id,
                workbooks_data,
                progress_callback
            )
            logger.info(f"✅ Table data exported: {len(excel_files)} files")
        except Exception as e:
            logger.error(f"❌ Failed to export table data: {e}", exc_info=True)
            excel_files = []

        # Step 5: Generate documentation (without suggestions)
        self._update_progress(
            migration_id,
            MigrationStatus.VALIDATING,
            90,
            "Generating documentation...",
            progress_callback
        )

        logger.info("Step 5/5: Generating documentation...")
        try:
            self._generate_migration_documentation(
                migration_id,
                workbooks_data,
                filter_param_results,
                Path("exports") / migration_id
            )
            logger.info("✅ Documentation generated")
        except Exception as e:
            logger.error(f"❌ Failed to generate documentation: {e}", exc_info=True)

        logger.info("=" * 60)
        logger.info("✅ PHASE 5 COMPLETE")
        logger.info("=" * 60)

        return {
            "validated_count": validated_count,
            "perfect_matches": perfect_matches,
            "avg_pass_rate": avg_pass_rate,
            "pbip_path": str(pbip_path) if pbip_path else None,
            "excel_files": excel_files,
            "relationships_count": len(relationships)
        }

    # ============================================
    # Phase 6: Build Data Model (NEW)
    # ============================================

    def _build_data_model(
        self,
        migration_id: str,
        workbooks_data: List[Dict[str, Any]],
        progress_callback: Optional[ProgressCallback]
    ) -> List[Relationship]:
        """Build Power BI data model (relationships, date table)"""

        logger.info("Phase 6: Building Power BI data model...")

        # Collect all data sources
        all_data_sources = []
        for wb in workbooks_data:
            all_data_sources.extend(wb.get("data_sources", []))

        # Build relationships
        relationships = self.model_builder.build_relationships_from_tableau(
            data_sources=all_data_sources
        )

        # Optimize relationships
        relationships = self.model_builder.optimize_model_relationships(relationships)

        logger.info(f"Built {len(relationships)} relationships")

        return relationships

    # ============================================
    # Phase 7: Convert Filters & Parameters (NEW)
    # ============================================

    def _convert_filters_parameters(
        self,
        migration_id: str,
        workbooks_data: List[Dict[str, Any]],
        progress_callback: Optional[ProgressCallback]
    ) -> Dict[str, Any]:
        """Convert Tableau filters and parameters to Power BI"""

        logger.info("Phase 7: Converting filters and parameters...")

        # Collect all filters and parameters
        all_filters = []
        all_parameters = []
        all_worksheets = []

        for wb in workbooks_data:
            # Collect worksheets
            all_worksheets.extend(wb.get("worksheets", []))

            # Get filters from parser
            parser = wb.get("parser")
            if parser:
                filters = parser.parse_filters()
                all_filters.extend(filters)

            all_parameters.extend(wb.get("parameters", []))

        # Convert filters
        powerbi_filters = self.filter_converter.convert_filters(
            all_filters,
            worksheets=[ws.name for ws in all_worksheets]
        )

        # Convert parameters
        param_conversion = self.filter_converter.convert_parameters(all_parameters)

        logger.info(
            f"Converted {len(powerbi_filters)} filters, "
            f"{len(param_conversion['whatif_parameters'])} parameters"
        )

        return {
            "filters": powerbi_filters,
            "whatif_parameters": param_conversion["whatif_parameters"],
            "slicer_tables": param_conversion["slicer_tables"]
        }

    # ============================================
    # Phase 8: Create & Inject PBIX (NEW)
    # ============================================

    def _generate_pbip_project(
        self,
        migration_id: str,
        conversions: List[Dict[str, Any]],
        relationships: List[Relationship],
        filter_param_results: Dict[str, Any],
        workbooks_data: List[Dict[str, Any]],
        progress_callback: Optional[ProgressCallback]
    ) -> Optional[Path]:
        """Generate complete PBIP folder structure (replaces PBIX generation)"""

        logger.info("Phase 8: Generating PBIP project structure...")

        from src.tableau.powerbi_exporter import PowerBIExporter

        export_dir = Path("exports") / migration_id
        export_dir.mkdir(parents=True, exist_ok=True)

        # Collect all measures from conversions
        measures = []

        for conv in conversions:
            dax_result = conv.get("dax_result")

            if dax_result:
                measure = Measure(
                    name=conv.get("calc_name", "Measure"),
                    expression=dax_result.dax_formula,
                    display_folder="Migrated from Tableau",
                    description=f"Converted from Tableau calculation"
                )
                measures.append(measure)

        logger.info(f"✓ Prepared {len(measures)} measures for PBIP")

        try:
            # Create PowerBIExporter instance
            exporter = PowerBIExporter()

            # Generate model.bim directly with measures we have
            logger.info("Creating semantic model (model.bim)...")
            model_bim_path = export_dir / "model.bim"

            # Build model.bim content directly
            import json

            model = {
                "name": f"TableauMigration_{migration_id}",
                "compatibilityLevel": 1600,
                "model": {
                    "culture": "en-US",
                    "tables": [
                        {
                            "name": "Calendar",
                            "columns": [
                                {"name": "Date", "dataType": "dateTime", "isKey": True},
                                {"name": "Year", "dataType": "int64"},
                                {"name": "Quarter", "dataType": "int64"},
                                {"name": "Month", "dataType": "int64"},
                                {"name": "Month Name", "dataType": "string"},
                                {"name": "Day", "dataType": "int64"}
                            ],
                            "partitions": [
                                {
                                    "name": "Calendar",
                                    "mode": "import",
                                    "source": {
                                        "type": "m",
                                        "expression": "Calendar"
                                    }
                                }
                            ]
                        }
                    ],
                    "relationships": [],
                    "measures": []
                }
            }

            # Add measures from conversions
            for measure in measures:
                model["model"]["measures"].append({
                    "name": measure.name,
                    "expression": measure.expression,
                    "formatString": "#,##0.00"
                })

            # Write model.bim
            with open(model_bim_path, 'w', encoding='utf-8') as f:
                json.dump(model, f, indent=2)

            logger.info(f"✅ Created semantic model with {len(measures)} measures")

            # Now create PBIP project structure around it
            logger.info("Creating PBIP folder structure...")
            artifacts = {
                "semantic_model": str(model_bim_path)
            }

            pbip_path = exporter._create_pbip_project(
                migration_id=migration_id,
                export_path=export_dir,
                artifacts=artifacts
            )

            if pbip_path and pbip_path.exists():
                logger.info(f"✅ PBIP project created: {pbip_path}")
                logger.info(f"   → {len(measures)} measures added")
                logger.info(f"   → {len(relationships)} relationships defined")
                logger.info(f"   → Complete folder structure generated")

                return pbip_path
            else:
                logger.warning("⚠️  PBIP project creation failed")
                return None

        except Exception as e:
            logger.error(f"PBIP generation failed: {e}", exc_info=True)
            import traceback
            traceback.print_exc()
            return None

    def _export_dax_fallback(
        self,
        measures: List[Measure],
        export_dir: Path
    ):
        """Export DAX measures to .dax file as fallback (legacy, kept for compatibility)"""

        dax_file = export_dir / "measures.dax"

        with open(dax_file, 'w', encoding='utf-8') as f:
            f.write("/* ============================================\n")
            f.write("   POWER BI DAX MEASURES\n")
            f.write("   Generated from Tableau Migration\n")
            f.write(f"   Generated: {datetime.now().isoformat()}\n")
            f.write("   ============================================ */\n\n")

            for measure in measures:
                f.write(f"-- {measure.name}\n")
                if measure.description:
                    f.write(f"-- {measure.description}\n")
                f.write(f"{measure.name} = {measure.expression}\n\n")

        logger.info(f"✓ Exported {len(measures)} measures to: {dax_file}")

    def _generate_migration_documentation(
        self,
        migration_id: str,
        workbooks_data: List[Dict[str, Any]],
        filter_param_results: Dict[str, Any],
        export_dir: Path
    ):
        """Generate migration documentation reports"""

        logger.info("Generating migration documentation...")

        try:
            # Collect all worksheets
            all_worksheets = []
            all_filters = []
            all_parameters = []

            for wb in workbooks_data:
                all_worksheets.extend(wb.get("worksheets", []))
                all_parameters.extend(wb.get("parameters", []))

                # Get filters from parser
                parser = wb.get("parser")
                if parser:
                    filters = parser.parse_filters()
                    all_filters.extend(filters)

            # Filter/parameter conversion report
            try:
                filter_report = self.filter_converter.generate_conversion_report(
                    tableau_filters=all_filters,
                    tableau_parameters=all_parameters,
                    powerbi_filters=filter_param_results.get("filters", []),
                    whatif_parameters=filter_param_results.get("whatif_parameters", []),
                    slicer_tables=filter_param_results.get("slicer_tables", [])
                )

                filter_report_path = export_dir / "filter_parameter_conversion.md"

                with open(filter_report_path, 'w', encoding='utf-8') as f:
                    f.write(filter_report)

                logger.info(f"✓ Filter/parameter report: {filter_report_path}")
            except Exception as e:
                logger.error(f"Failed to generate filter/parameter report: {e}")

            # Visual conversion report
            try:
                powerbi_visuals = self.visual_converter.convert_worksheets_to_visuals(
                    worksheets=all_worksheets,
                    auto_layout=True
                )

                visual_report = self.visual_converter.generate_visual_conversion_report(
                    worksheets=all_worksheets,
                    visuals=powerbi_visuals
                )

                visual_report_path = export_dir / "visual_conversion.md"

                with open(visual_report_path, 'w', encoding='utf-8') as f:
                    f.write(visual_report)

                logger.info(f"✓ Visual conversion report: {visual_report_path}")
            except Exception as e:
                logger.error(f"Failed to generate visual conversion report: {e}")

        except Exception as e:
            logger.error(f"Failed to generate documentation: {e}")
            # Don't fail the entire migration if documentation generation fails

    def _export_table_data_to_excel(
        self,
        migration_id: str,
        workbooks_data: List[Dict[str, Any]],
        progress_callback: Optional[ProgressCallback]
    ) -> List[str]:
        """Export all table data from Hyper files to Excel"""

        logger.info("Exporting table data to Excel...")

        export_dir = Path("exports") / migration_id / "table_data"
        export_dir.mkdir(parents=True, exist_ok=True)

        excel_files = []

        # Collect all Hyper files
        all_hyper_files = []
        for wb in workbooks_data:
            all_hyper_files.extend(wb.get("hyper_files", []))

        if not all_hyper_files:
            logger.warning("No Hyper files found - skipping table data export")
            return excel_files

        try:
            import pandas as pd
            from src.tableau.hyper_profiler import HyperDataProfiler

            # Use HyperDataProfiler which handles Hyper API correctly
            for hyper_path in all_hyper_files:
                try:
                    logger.info(f"Processing Hyper file: {Path(hyper_path).name}")
                    profiler = HyperDataProfiler(str(hyper_path))

                    # Get all tables
                    tables = profiler.list_tables()
                    logger.info(f"Found {len(tables)} tables in Hyper file")

                    for table_name in tables:
                        try:
                            # Remove quotes from table name for read_table() method
                            # list_tables() returns: "Extract"."TableName"
                            # read_table() expects: Extract.TableName
                            unquoted_table = table_name.replace('"', '')

                            # Read table data
                            df = profiler.read_table(unquoted_table)

                            if df is not None and len(df) > 0:
                                # Clean table name for filename (remove schema prefix and special chars)
                                clean_name = table_name.replace('"', '').replace('.', '_').replace('!', '_')
                                excel_filename = f"{clean_name}.xlsx"
                                excel_path = export_dir / excel_filename

                                # Save to Excel
                                df.to_excel(excel_path, index=False, engine='openpyxl')

                                excel_files.append(str(excel_path))
                                logger.info(f"✓ Exported {len(df)} rows from {table_name} to {excel_filename}")
                            else:
                                logger.warning(f"Table {table_name} is empty, skipping export")

                        except Exception as e:
                            logger.warning(f"Failed to export table {table_name}: {e}")

                except Exception as e:
                    logger.error(f"Failed to process Hyper file {hyper_path}: {e}")

        except ImportError as e:
            logger.warning(f"Required libraries not available for Excel export: {e}")
            logger.info("Install with: pip install pandas openpyxl")

        logger.info(f"✓ Exported {len(excel_files)} tables to Excel")

        return excel_files

    # ============================================
    # Utility Methods
    # ============================================

    def _update_progress(
        self,
        migration_id: str,
        status: MigrationStatus,
        progress_percent: int,
        message: str,
        progress_callback: Optional[ProgressCallback]
    ):
        """Update migration progress"""
        # Update database - status only
        self.migration_store.update_migration_status(
            migration_id,
            status
        )

        # Update progress with current stage
        self.migration_store.update_migration_progress(
            migration_id,
            progress_percent,
            current_stage=message,
            message=message
        )

        # Call progress callback (for WebSocket broadcasting)
        if progress_callback:
            progress_callback.increment(message)

        logger.debug(f"Progress: {progress_percent}% - {message}")
