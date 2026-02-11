"""
Database Migration Script: Add depends_on and depends_on_metadata columns

This script adds the new dependency metadata columns to existing databases.

Usage:
    python bknd/migrate_database.py
"""

import sqlite3
from pathlib import Path
import sys
import os


def migrate_database():
    """Add depends_on and depends_on_metadata columns to tableau_calculations table"""

    # Find database path - use relative path from bknd directory
    db_path = Path(__file__).parent / "data" / "jobs.db"

    print(f"Migrating database at: {db_path}")

    if not Path(db_path).exists():
        print(f"ERROR: Database file not found: {db_path}")
        return False

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Check if columns already exist
        cursor.execute("PRAGMA table_info(tableau_calculations)")
        columns = {row[1] for row in cursor.fetchall()}

        print(f"Current tableau_calculations columns: {columns}")

        needs_migration = False

        # Add depends_on column if it doesn't exist
        if "depends_on" not in columns:
            print("Adding 'depends_on' column...")
            cursor.execute("""
                ALTER TABLE tableau_calculations
                ADD COLUMN depends_on TEXT
            """)
            print("SUCCESS: Added 'depends_on' column")
            needs_migration = True
        else:
            print("'depends_on' column already exists")

        # Add depends_on_metadata column if it doesn't exist
        if "depends_on_metadata" not in columns:
            print("Adding 'depends_on_metadata' column...")
            cursor.execute("""
                ALTER TABLE tableau_calculations
                ADD COLUMN depends_on_metadata TEXT
            """)
            print("SUCCESS: Added 'depends_on_metadata' column")
            needs_migration = True
        else:
            print("'depends_on_metadata' column already exists")

        if needs_migration:
            conn.commit()
            print("SUCCESS: Database migration completed successfully!")
        else:
            print("No migration needed - database already up to date")

        # Verify the migration
        cursor.execute("PRAGMA table_info(tableau_calculations)")
        columns_after = {row[1] for row in cursor.fetchall()}
        print(f"Columns after migration: {columns_after}")

        conn.close()
        return True

    except Exception as e:
        print(f"ERROR: Migration failed: {e}")
        import traceback
        traceback.print_exc()
        if 'conn' in locals():
            conn.rollback()
            conn.close()
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("DATABASE MIGRATION: Add Dependency Metadata Columns")
    print("=" * 60)

    success = migrate_database()

    if success:
        print("\nSUCCESS: Migration completed successfully!")
        print("\nYou can now restart your FastAPI server.")
    else:
        print("\nERROR: Migration failed!")
        print("Please check the error messages above.")
        sys.exit(1)
