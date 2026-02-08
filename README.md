# Excel Relationship Discovery System

A production-ready system that discovers relationships between N Excel files from **any business data source** by combining deterministic profiling (80%) with LLM semantic reasoning (20%). Works across all industries: e-commerce, healthcare, finance, manufacturing, and more.

## Features

- **30+ Relationship Detection Cases**: Exact matches, semantic similarity, format mismatches, composite keys, temporal joins, and more
- **Hybrid Approach**: Deterministic profiling + LLM validation (Llama-3.3B via Azure AI Foundry)
- **Domain-Agnostic Business Validation**: Analyzes relationship validity, data coherence, and actionable insights for ANY business domain
- **Comprehensive Profiling**: Deep statistical and semantic analysis of every column
- **JSON Report Output**: Detailed report with confidence scores, business insights, and recommendations
- **Optimized for 5 Files**: Fast processing with intelligent caching

## Quick Start

### 1. Installation

```bash
# Clone or download this repository
cd excel-r

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# Azure AI Foundry Configuration
AZURE_FOUNDRY_ENDPOINT=https://your-endpoint.azure.com
AZURE_FOUNDRY_API_KEY=your_api_key_here
AZURE_FOUNDRY_MODEL=llama-3.3b

# Optional: Disable LLM validation
# ENABLE_LLM_VALIDATION=false
```

### 3. Run Discovery

```bash
# Basic usage
python -m src.main file1.xlsx file2.xlsx file3.xlsx

# Specify output file
python -m src.main file1.xlsx file2.xlsx -o output/report.json

# Disable LLM validation
python -m src.main file1.xlsx file2.xlsx --no-llm
```

### 4. Output

The system generates a comprehensive JSON report:

```json
{
  "report_metadata": {
    "generated_at": "2024-01-29T14:30:00",
    "file_count": 3,
    "total_relationships_found": 12,
    "high_confidence": 8,
    "medium_confidence": 3,
    "low_confidence": 1,
    "processing_time_seconds": 45.2
  },
  "files": [...],
  "relationships": [
    {
      "relationship_id": "REL_001",
      "source": {"file": "orders.xlsx", "column": "customer_id"},
      "target": {"file": "customers.xlsx", "column": "customer_id"},
      "relationship_type": "PRIMARY_KEY -> FOREIGN_KEY",
      "confidence_level": "HIGH",
      "confidence_score": 95,
      "statistics": {
        "value_overlap_percent": 97.9,
        "orphans_in_source": 210
      }
    }
  ]
}
```

## Architecture

```
Excel Files → Data Loader → Profiling Engine → Relationship Detector 
                                                       ↓
                                                 LLM Validator
                                                       ↓
Final JSON Report ← Report Generator ← Validation Layer
```

## Relationship Detection Categories

1. **Direct Key Matching** (HIGH confidence)
   - Exact name + data type match
   - Name variations (CustomerID vs customer_id)
   - Abbreviation expansion (cust_id vs customer_id)

2. **Semantic Similarity** (MEDIUM confidence, requires LLM)
   - Synonym matching (order_date vs transaction_date)
   - Business logic equivalence (revenue vs net_sales)

3. **Format Mismatches** (MEDIUM confidence)
   - Prefix/suffix differences (CUST-001 vs 001)
   - Case sensitivity (USA vs usa)
   - Date format differences

4. **Complex Cases** (Handled automatically)
   - Composite keys
   - Temporal range joins
   - Many-to-many relationships
   - Hierarchical relationships

## Configuration

All settings are in `src/config.py`:

```python
# Key thresholds
HIGH_CONFIDENCE_OVERLAP_THRESHOLD = 0.80  # 80%
UNIQUE_THRESHOLD_FOR_PK = 0.95            # 95%

# LLM settings
ENABLE_LLM_VALIDATION = True
LLM_MODEL = "llama-3.3b"

# Performance
MAX_FILES_LIMIT = 5
MAX_WORKERS = 3
```

## Project Structure

```
excel-r/
├── src/
│   ├── main.py                    # Main orchestrator
│   ├── config.py                  # Configuration
│   ├── excel_loader.py            # File loading
│   ├── profiling_engine.py        # Data profiling
│   ├── relationship_detector.py   # Relationship detection
│   ├── llm_reasoner.py           # LLM integration
│   └── utils/
│       ├── data_types.py          # Type inference
│       └── pattern_matching.py    # Pattern utilities
├── output/                        # Generated reports
├── requirements.txt
├── .env.example
└── README.md
```

## Azure AI Foundry Setup

See [azure_foundry_setup.md](./azure_foundry_setup.md) for detailed instructions on:
- Deploying Llama-3.3B in Azure AI Foundry
- Authentication and endpoints
- Testing the connection

## Example Use Cases

### E-Commerce Analysis

```bash
python -m src.main \
  data/orders.xlsx \
  data/customers.xlsx \
  data/products.xlsx \
  data/shipments.xlsx
```

### Healthcare Data Integration

```bash
python -m src.main \
  data/patients.xlsx \
  data/visits.xlsx \
  data/treatments.xlsx \
  data/billing.xlsx
```

### Financial Services Analysis

```bash
python -m src.main \
  data/accounts.xlsx \
  data/transactions.xlsx \
  data/customers.xlsx
```

### Data Quality Issues

Identifies and warns about:
- Orphan records (FK without PK)
- NULL values in key columns
- Duplicate primary keys
- Row explosion risks

## Performance

- **5 files, 100K rows each**: ~2 minutes
- **Caching enabled**: Second run ~30 seconds
- **LLM calls**: Only for ambiguous cases (typically 10-20% of candidates)

## Troubleshooting

### LLM Connection Issues

```bash
# Test Azure AI Foundry connection
python -c "from src.llm_reasoner import LLMReasoner; LLMReasoner().test_connection()"
```

### Disable LLM

If Azure AI Foundry is not available, disable LLM validation:

```bash
export ENABLE_LLM_VALIDATION=false
# or use --no-llm flag
python -m src.main file1.xlsx file2.xlsx --no-llm
```

### Large Files

For files >100K rows:

```bash
# Increase sample size in config.py
SAMPLE_SIZE_FOR_PROFILING = 50000
```

## License

MIT License

## Support

For issues or questions, please refer to the implementation plan and documentation in the repository.
