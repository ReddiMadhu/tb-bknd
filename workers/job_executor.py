"""Background job executor for relationship discovery"""
import threading
from typing import List, Dict
from loguru import logger
import pandas as pd
import pickle

from src.main import RelationshipDiscovery
from src.profiling_engine import ProfilingEngine
from src.relationship_detector import RelationshipDetector
from storage.job_store import JobStore
from storage.result_store import ResultStore
from workers.progress_manager import DatabaseProgressCallback
from workers.websocket_manager import ws_manager
from api.models.api_models import JobStatus
import asyncio


# Thread pool for background jobs
from concurrent.futures import ThreadPoolExecutor
executor = ThreadPoolExecutor(max_workers=3)


def execute_discovery_job(job_id: str, file_paths: List[str]):
    """
    Execute discovery job in background thread

    Args:
        job_id: Job identifier
        file_paths: List of file paths to analyze
    """
    job_store = JobStore()
    result_store = ResultStore()

    try:
        logger.info(f"Starting job {job_id} with {len(file_paths)} files")

        # Update status to running
        job_store.update_status(job_id, JobStatus.RUNNING)

        # Create progress callback
        progress_callback = DatabaseProgressCallback(job_id)

        # Create discovery instance
        discovery = RelationshipDiscovery()

        # Run discovery with progress tracking
        result = discovery.discover_relationships(
            file_paths=file_paths,
            output_file=None,  # We'll save it ourselves
            progress_callback=progress_callback
        )

        # Save result
        result_file_path = result_store.save_result(job_id, result)

        # Get relationship count
        relationship_count = len(result.get("relationships", []))

        # Update job status to completed
        job_store.update_status(
            job_id,
            JobStatus.COMPLETED,
            relationship_count=relationship_count,
            result_file_path=result_file_path
        )

        # Broadcast completion
        _broadcast_completion(job_id, relationship_count)

        logger.info(f"Job {job_id} completed successfully. Found {relationship_count} relationships")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)

        # Update job status to failed
        job_store.update_status(
            job_id,
            JobStatus.FAILED,
            error=str(e)
        )

        # Broadcast error
        _broadcast_error(job_id, str(e))


def _broadcast_completion(job_id: str, relationship_count: int):
    """Broadcast job completion via WebSocket"""
    try:
        # Create event loop if not exists
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        # Run async broadcast
        if not loop.is_running():
            loop.run_until_complete(
                ws_manager.broadcast_completion(job_id, relationship_count)
            )
        else:
            asyncio.create_task(
                ws_manager.broadcast_completion(job_id, relationship_count)
            )
    except Exception as e:
        logger.error(f"Failed to broadcast completion: {e}")


def _broadcast_error(job_id: str, error_message: str):
    """Broadcast job error via WebSocket"""
    try:
        # Create event loop if not exists
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        # Run async broadcast
        if not loop.is_running():
            loop.run_until_complete(
                ws_manager.broadcast_error(job_id, error_message)
            )
        else:
            asyncio.create_task(
                ws_manager.broadcast_error(job_id, error_message)
            )
    except Exception as e:
        logger.error(f"Failed to broadcast error: {e}")


def execute_tableau_discovery_job(job_id: str, dataframes: Dict[str, pd.DataFrame]):
    """
    Execute discovery job for Tableau data (DataFrames instead of Excel files).

    Args:
        job_id: Job identifier
        dataframes: Dictionary mapping table names to DataFrames
    """
    job_store = JobStore()
    result_store = ResultStore()

    try:
        logger.info(f"Starting Tableau discovery job {job_id} with {len(dataframes)} tables")

        # Update status to running
        job_store.update_status(job_id, JobStatus.RUNNING)

        # Create progress callback
        progress_callback = DatabaseProgressCallback(job_id)

        # Convert Name objects to strings (Hyper API returns Name objects for column names)
        cleaned_dataframes = {}
        for table_name, df in dataframes.items():
            # Convert column names to strings
            df_cleaned = df.copy()
            df_cleaned.columns = [str(col) for col in df_cleaned.columns]
            cleaned_dataframes[str(table_name)] = df_cleaned

        logger.info(f"Cleaned {len(cleaned_dataframes)} DataFrames for profiling")

        # Profile DataFrames directly
        logger.info("Profiling Tableau data...")
        profiler = ProfilingEngine()
        profiles = profiler.profile_all_files(cleaned_dataframes)

        # Detect relationships
        logger.info("Detecting relationships...")
        detector = RelationshipDetector(profiles, cleaned_dataframes)
        candidates = detector.generate_candidates()

        # Format result - Match Excel workflow format for frontend compatibility
        relationships = []
        for candidate in candidates:
            relationships.append({
                "relationship_id": f"{candidate.source_file}_{candidate.source_column}_{candidate.target_file}_{candidate.target_column}",
                "source": {
                    "file": candidate.source_file,
                    "column": candidate.source_column
                },
                "target": {
                    "file": candidate.target_file,
                    "column": candidate.target_column
                },
                "relationship_type": candidate.relationship_type,
                "confidence_score": candidate.confidence_score,
                "confidence_level": candidate.confidence_level,
                "detection_method": candidate.detection_method,
                "statistics": candidate.statistics,
                "deleted": False
            })

        # Create files metadata for frontend graph visualization
        files = []
        for table_name, df in cleaned_dataframes.items():
            columns = []
            for col in df.columns:
                columns.append({
                    "column_name": col,
                    "data_type": str(df[col].dtype),
                    "is_primary_key": False,  # Could be enhanced later
                    "is_foreign_key": False
                })

            files.append({
                "file_name": table_name,
                "file_path": table_name,
                "sheet_name": table_name,
                "row_count": len(df),
                "column_count": len(df.columns),
                "columns": columns
            })

        result = {
            "files": files,
            "relationships": relationships,
            "table_count": len(dataframes),
            "total_candidates": len(candidates)
        }

        # Save result
        result_file_path = result_store.save_result(job_id, result)

        # Update job status to completed
        job_store.update_status(
            job_id,
            JobStatus.COMPLETED,
            relationship_count=len(relationships),
            result_file_path=result_file_path
        )

        # Broadcast completion
        _broadcast_completion(job_id, len(relationships))

        logger.info(f"Tableau job {job_id} completed successfully. Found {len(relationships)} relationships")

    except Exception as e:
        logger.error(f"Tableau job {job_id} failed: {e}", exc_info=True)

        # Update job status to failed
        job_store.update_status(
            job_id,
            JobStatus.FAILED,
            error=str(e)
        )

        # Broadcast error
        _broadcast_error(job_id, str(e))
