"""
PBIP Structure Verification Script

Run this after a migration to verify the generated PBIP folder structure
matches Microsoft's specification.

Usage:
    python verify_pbip_structure.py <migration_id>

Example:
    python verify_pbip_structure.py mig_abc123
"""

import json
import sys
from pathlib import Path
from typing import List, Tuple


class PBIPValidator:
    """Validate PBIP folder structure against Microsoft spec"""

    def __init__(self, migration_id: str):
        self.migration_id = migration_id
        self.export_dir = Path("exports") / migration_id
        self.pbip_root = self.export_dir / f"{migration_id}.pbip"
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: List[str] = []

    def validate(self) -> bool:
        """Run all validations. Returns True if PBIP structure is valid."""
        print(f"\n{'='*60}")
        print(f"🔍 Validating PBIP Structure for: {self.migration_id}")
        print(f"{'='*60}\n")

        # Check if export directory exists
        if not self.export_dir.exists():
            self.errors.append(f"Export directory not found: {self.export_dir}")
            self._print_results()
            return False

        # Check if PBIP root folder exists
        if not self.pbip_root.exists():
            self.errors.append(f"PBIP root folder not found: {self.pbip_root}")
            self._print_results()
            return False

        # Run all checks
        self._check_root_structure()
        self._check_root_pbip_file()
        self._check_report_folder()
        self._check_semantic_model_folder()
        self._check_gitignore()
        self._check_file_contents()

        # Print results
        self._print_results()

        return len(self.errors) == 0

    def _check_root_structure(self):
        """Verify root folder structure"""
        print("📁 Checking root structure...")

        expected_items = [
            (f"{self.migration_id}.pbip", "file"),
            (f"{self.migration_id}.Report", "folder"),
            (f"{self.migration_id}.SemanticModel", "folder"),
            (".gitignore", "file")
        ]

        for item_name, item_type in expected_items:
            item_path = self.pbip_root / item_name

            if item_type == "file":
                if not item_path.is_file():
                    self.errors.append(f"Missing required file: {item_name}")
                else:
                    self.info.append(f"✓ Found {item_name}")

            elif item_type == "folder":
                if not item_path.is_dir():
                    self.errors.append(f"Missing required folder: {item_name}")
                else:
                    self.info.append(f"✓ Found {item_name}/")

    def _check_root_pbip_file(self):
        """Verify root .pbip file structure"""
        print("📄 Checking root .pbip file...")

        pbip_file = self.pbip_root / f"{self.migration_id}.pbip"

        if not pbip_file.exists():
            return  # Already flagged in root structure check

        try:
            with open(pbip_file, 'r', encoding='utf-8') as f:
                content = json.load(f)

            # Check version
            if "version" not in content:
                self.errors.append(".pbip file missing 'version' field")

            # Check artifacts
            if "artifacts" not in content:
                self.errors.append(".pbip file missing 'artifacts' field")
            else:
                artifacts = content["artifacts"]

                # Should have exactly ONE artifact (report only)
                # The report's definition.pbir file links to the semantic model
                if len(artifacts) != 1:
                    self.warnings.append(f".pbip has {len(artifacts)} artifacts (expected 1: report only)")

                has_report = any("report" in a for a in artifacts)

                if not has_report:
                    self.errors.append(".pbip artifacts missing 'report' entry")
                else:
                    self.info.append("✓ .pbip has report artifact")

                # Check if incorrectly has dataset artifact (should be in definition.pbir instead)
                has_dataset = any("dataset" in a for a in artifacts)
                if has_dataset:
                    self.errors.append(".pbip should NOT have 'dataset' artifact (use report's definition.pbir instead)")

        except json.JSONDecodeError as e:
            self.errors.append(f".pbip file is not valid JSON: {e}")

    def _check_report_folder(self):
        """Verify Report folder structure"""
        print("📊 Checking Report folder...")

        report_folder = self.pbip_root / f"{self.migration_id}.Report"

        if not report_folder.exists():
            return  # Already flagged

        required_files = [
            "definition.pbir",
            "report.json",
            "semanticModelDiagramLayout.json",
            ".pbi/localSettings.json"
        ]

        for file_path in required_files:
            full_path = report_folder / file_path

            if not full_path.exists():
                self.errors.append(f"Missing Report file: {file_path}")
            else:
                self.info.append(f"✓ Found Report/{file_path}")

    def _check_semantic_model_folder(self):
        """Verify SemanticModel folder structure"""
        print("🗂️  Checking SemanticModel folder...")

        sm_folder = self.pbip_root / f"{self.migration_id}.SemanticModel"

        if not sm_folder.exists():
            return  # Already flagged

        required_files = [
            "definition.pbism",
            "model.bim",
            "diagramLayout.json",
            ".pbi/localSettings.json"
        ]

        for file_path in required_files:
            full_path = sm_folder / file_path

            if not full_path.exists():
                self.errors.append(f"Missing SemanticModel file: {file_path}")
            else:
                self.info.append(f"✓ Found SemanticModel/{file_path}")

    def _check_gitignore(self):
        """Check .gitignore content"""
        print("🚫 Checking .gitignore...")

        gitignore_path = self.pbip_root / ".gitignore"

        if not gitignore_path.exists():
            return

        try:
            with open(gitignore_path, 'r') as f:
                content = f.read()

            # Check for essential patterns
            essential_patterns = [
                "localSettings.json",
                ".cache.abf"
            ]

            for pattern in essential_patterns:
                if pattern in content:
                    self.info.append(f"✓ .gitignore includes {pattern}")
                else:
                    self.warnings.append(f".gitignore missing pattern: {pattern}")

        except Exception as e:
            self.warnings.append(f"Could not read .gitignore: {e}")

    def _check_file_contents(self):
        """Verify critical file contents"""
        print("📝 Checking file contents...")

        # Check model.bim
        model_bim = self.pbip_root / f"{self.migration_id}.SemanticModel" / "model.bim"

        if model_bim.exists():
            try:
                with open(model_bim, 'r', encoding='utf-8') as f:
                    model = json.load(f)

                # Check for required fields
                if "name" in model:
                    self.info.append(f"✓ model.bim has name: {model['name']}")

                if "compatibilityLevel" in model:
                    self.info.append(f"✓ model.bim compatibility level: {model['compatibilityLevel']}")

                # Check for measures
                if "model" in model and "measures" in model["model"]:
                    measure_count = len(model["model"]["measures"])
                    if measure_count > 0:
                        self.info.append(f"✓ model.bim contains {measure_count} measures")
                    else:
                        self.warnings.append("model.bim has no measures")
                else:
                    self.errors.append("model.bim missing 'model.measures' structure")

            except json.JSONDecodeError as e:
                self.errors.append(f"model.bim is not valid JSON: {e}")

        # Check definition.pbism
        definition = self.pbip_root / f"{self.migration_id}.SemanticModel" / "definition.pbism"

        if definition.exists():
            try:
                with open(definition, 'r', encoding='utf-8') as f:
                    def_content = json.load(f)

                if "version" in def_content:
                    self.info.append(f"✓ definition.pbism version: {def_content['version']}")

                if "name" in def_content:
                    self.info.append(f"✓ definition.pbism name: {def_content['name']}")

            except json.JSONDecodeError as e:
                self.errors.append(f"definition.pbism is not valid JSON: {e}")

    def _print_results(self):
        """Print validation results"""
        print(f"\n{'='*60}")
        print("📋 VALIDATION RESULTS")
        print(f"{'='*60}\n")

        # Print errors
        if self.errors:
            print(f"❌ ERRORS ({len(self.errors)}):")
            for error in self.errors:
                print(f"   • {error}")
            print()

        # Print warnings
        if self.warnings:
            print(f"⚠️  WARNINGS ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"   • {warning}")
            print()

        # Print info (only if no errors)
        if not self.errors and self.info:
            print(f"ℹ️  INFO ({len(self.info)} checks passed):")
            for info in self.info[:10]:  # Show first 10
                print(f"   • {info}")

            if len(self.info) > 10:
                print(f"   ... and {len(self.info) - 10} more")
            print()

        # Final verdict
        if not self.errors:
            print("✅ PBIP STRUCTURE VALID - Ready to open in Power BI Desktop!")
        else:
            print(f"❌ PBIP STRUCTURE INVALID - {len(self.errors)} errors found")

        print(f"\n{'='*60}\n")


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python verify_pbip_structure.py <migration_id>")
        print("\nExample: python verify_pbip_structure.py mig_abc123")
        sys.exit(1)

    migration_id = sys.argv[1]

    validator = PBIPValidator(migration_id)
    is_valid = validator.validate()

    # Exit with appropriate code
    sys.exit(0 if is_valid else 1)


if __name__ == "__main__":
    main()
