"""
Debug Script: Check if dependency metadata is being saved
"""
import sqlite3
from pathlib import Path
import json

# Connect to database
db_path = Path(__file__).parent / "data" / "jobs.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("=" * 80)
print("CHECKING DEPENDENCY METADATA IN DATABASE")
print("=" * 80)

# Query 1: Check if columns exist
print("\n1. Checking if metadata columns exist...")
cursor.execute("PRAGMA table_info(tableau_calculations)")
columns = {row[1] for row in cursor.fetchall()}
print(f"Columns: {columns}")

if "depends_on_metadata" in columns:
    print("✅ depends_on_metadata column exists")
else:
    print("❌ depends_on_metadata column MISSING!")

# Query 2: Check recent calculations
print("\n2. Checking recent calculations with metadata...")
cursor.execute("""
    SELECT
        calc_name,
        SUBSTR(calc_formula, 1, 80) as formula_preview,
        depends_on,
        depends_on_metadata,
        created_at
    FROM tableau_calculations
    ORDER BY created_at DESC
    LIMIT 10
""")

rows = cursor.fetchall()
print(f"\nFound {len(rows)} recent calculations:\n")

for i, row in enumerate(rows, 1):
    calc_name, formula, depends_on, metadata, created_at = row

    print(f"--- Calculation {i} ---")
    print(f"Name: {calc_name}")
    print(f"Formula: {formula}...")
    print(f"Depends On: {depends_on}")

    if metadata:
        print(f"Metadata: {metadata[:200]}...")  # First 200 chars
        try:
            meta_obj = json.loads(metadata)
            print(f"Metadata Keys: {list(meta_obj.keys())}")
        except:
            print("(Could not parse metadata JSON)")
    else:
        print("Metadata: NULL ❌")

    print(f"Created: {created_at}")
    print()

# Query 3: Check for calculations that reference other calculations
print("\n3. Checking calculations that reference other calculations...")
cursor.execute("""
    SELECT
        calc_name,
        calc_formula,
        depends_on,
        depends_on_metadata
    FROM tableau_calculations
    WHERE calc_formula LIKE '%Calculation_%'
       OR calc_formula LIKE '%sum([%'
    ORDER BY created_at DESC
    LIMIT 5
""")

rows = cursor.fetchall()
print(f"\nFound {len(rows)} calculations with references:\n")

for calc_name, formula, depends_on, metadata in rows:
    print(f"Name: {calc_name}")
    print(f"Formula: {formula[:100]}...")
    print(f"Depends On: {depends_on}")

    if metadata:
        try:
            meta_obj = json.loads(metadata)
            print(f"Metadata fields: {list(meta_obj.keys())}")
            for field, field_meta in meta_obj.items():
                print(f"  - {field}: {field_meta.get('field_type', 'UNKNOWN')}")
        except Exception as e:
            print(f"Metadata parse error: {e}")
    else:
        print("❌ NO METADATA SAVED")
    print("-" * 80)

conn.close()

print("\n" + "=" * 80)
print("DEBUG COMPLETE")
print("=" * 80)
