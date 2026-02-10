"""Job management API endpoints"""
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Query
from typing import List, Optional
from datetime import datetime
from loguru import logger
import json

from api.models.api_models import (
    JobCreateResponse,
    JobStatusResponse,
    JobResultResponse,
    JobListResponse,
    JobListItem,
    JobDeleteResponse,
    JobStatus,
    ErrorResponse,
    # Preview models
    JobPreviewResponse,
    FilePreview,
    ColumnPreview,
    DuplicateGroupInfo,
    JobConfirmRequest,
    JobConfirmResponse,
    PreviewDeleteResponse,
    FileColumnSelection
)
from api.utils import generate_job_id, generate_preview_id
from api.config import config
from storage.job_store import JobStore
from storage.file_store import FileStore
from storage.result_store import ResultStore
from storage.preview_store import PreviewStore
from storage.database import get_db_connection

# Import job executor (will create next)
from workers.job_executor import execute_discovery_job, execute_tableau_discovery_job

router = APIRouter()

# Initialize stores
job_store = JobStore()
file_store = FileStore()
result_store = ResultStore()
preview_store = PreviewStore()


@router.post("/", response_model=JobCreateResponse, status_code=201)
async def create_job(
    files: List[UploadFile] = File(..., description="Excel files to analyze (1-5 files)"),
    background_tasks: BackgroundTasks = None
):
    """
    Create a new relationship discovery job

    Upload 1-5 Excel files for analysis. The job will be processed in the background.
    Use the returned job_id to check status and retrieve results.
    """
    try:
        # Validate file count
        if len(files) < 1:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "INVALID_FILE_COUNT",
                        "message": "At least 1 file is required",
                        "details": {"min_files": 1}
                    }
                }
            )

        if len(files) > config.MAX_FILES_PER_JOB:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "TOO_MANY_FILES",
                        "message": f"Maximum {config.MAX_FILES_PER_JOB} files allowed",
                        "details": {
                            "max_files": config.MAX_FILES_PER_JOB,
                            "provided": len(files)
                        }
                    }
                }
            )

        # Generate job ID
        job_id = generate_job_id()

        # Create job in database FIRST (before saving files due to foreign key constraint)
        job = job_store.create_job(job_id=job_id, file_count=len(files))

        # Validate and save files
        file_paths = []
        for file in files:
            # Read file content
            content = await file.read()
            file_size = len(content)

            # Validate file
            is_valid, error_msg = file_store.validate_file(file.filename, file_size)
            if not is_valid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "code": "INVALID_FILE",
                            "message": error_msg,
                            "details": {"filename": file.filename}
                        }
                    }
                )

            # Save file
            uploaded_file = file_store.save_uploaded_file(
                job_id=job_id,
                original_filename=file.filename,
                file_content=content
            )
            file_paths.append(uploaded_file.file_path)

        # Schedule background job
        background_tasks.add_task(
            execute_discovery_job,
            job_id=job_id,
            file_paths=file_paths
        )

        logger.info(f"Created job {job_id} with {len(files)} files")

        return JobCreateResponse(
            job_id=job.job_id,
            status=job.status,
            created_at=job.created_at,
            file_count=job.file_count,
            message="Job created successfully and processing started"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create job: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "JOB_CREATION_FAILED",
                    "message": "Failed to create job",
                    "details": str(e)
                }
            }
        )


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """
    Get job status and progress

    Returns current status, progress percentage, and processing stage.
    """
    job = job_store.get_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "JOB_NOT_FOUND",
                    "message": f"Job {job_id} not found",
                    "details": {"job_id": job_id}
                }
            }
        )

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress_percent=job.progress_percent,
        current_stage=job.current_stage,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        file_count=job.file_count,
        relationships_found=job.relationship_count,
        error=job.error_message
    )


@router.get("/{job_id}/result", response_model=JobResultResponse)
async def get_job_result(job_id: str):
    """
    Get job analysis results

    Returns the full JSON report if the job is completed.
    Returns 202 Accepted if the job is still running.
    """
    job = job_store.get_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "JOB_NOT_FOUND",
                    "message": f"Job {job_id} not found",
                    "details": {"job_id": job_id}
                }
            }
        )

    # If job is still running, return 202 Accepted
    if job.status in [JobStatus.PENDING, JobStatus.RUNNING]:
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job.job_id,
                "status": job.status.value,
                "progress_percent": job.progress_percent,
                "message": "Job is still processing"
            }
        )

    # If job failed, return error
    if job.status == JobStatus.FAILED:
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "JOB_FAILED",
                    "message": "Job execution failed",
                    "details": {
                        "job_id": job_id,
                        "error": job.error_message
                    }
                }
            }
        )

    # Job completed - return result
    result = result_store.get_result(job_id)

    if not result:
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "RESULT_NOT_FOUND",
                    "message": "Job completed but result not found",
                    "details": {"job_id": job_id}
                }
            }
        )

    return JobResultResponse(
        job_id=job.job_id,
        status=job.status,
        result=result,
        completed_at=job.completed_at,
        message=None
    )


@router.get("/", response_model=JobListResponse)
async def list_jobs(
    limit: int = Query(20, ge=1, le=100, description="Number of jobs to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    status: Optional[str] = Query(None, description="Filter by status")
):
    """
    List all jobs with pagination

    Returns paginated list of jobs, optionally filtered by status.
    """
    try:
        jobs, total = job_store.list_jobs(limit=limit, offset=offset, status=status)

        job_items = [
            JobListItem(
                job_id=job.job_id,
                status=job.status,
                created_at=job.created_at,
                completed_at=job.completed_at,
                file_count=job.file_count,
                relationships_found=job.relationship_count,
                progress_percent=job.progress_percent
            )
            for job in jobs
        ]

        return JobListResponse(
            total=total,
            limit=limit,
            offset=offset,
            jobs=job_items
        )

    except Exception as e:
        logger.error(f"Failed to list jobs: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "LIST_JOBS_FAILED",
                    "message": "Failed to list jobs",
                    "details": str(e)
                }
            }
        )


@router.delete("/{job_id}", response_model=JobDeleteResponse)
async def delete_job(job_id: str):
    """
    Delete a job and all associated files

    Cancels the job if it's running and deletes all uploaded files and results.
    """
    job = job_store.get_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "JOB_NOT_FOUND",
                    "message": f"Job {job_id} not found",
                    "details": {"job_id": job_id}
                }
            }
        )

    try:
        # Delete files
        files_deleted = 0
        if config.DELETE_FILES_ON_JOB_DELETE:
            files_deleted = file_store.delete_job_files(job_id)
            result_store.delete_result(job_id)

        # Delete job from database
        job_store.delete_job(job_id)

        logger.info(f"Deleted job {job_id}")

        return JobDeleteResponse(
            message=f"Job {job_id} deleted successfully",
            job_id=job_id,
            files_deleted=files_deleted
        )

    except Exception as e:
        logger.error(f"Failed to delete job {job_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "DELETE_FAILED",
                    "message": "Failed to delete job",
                    "details": str(e)
                }
            }
        )


# ==================== Preview Endpoints ====================

@router.post("/preview", response_model=JobPreviewResponse, status_code=201)
async def create_preview(
    files: List[UploadFile] = File(..., description="Excel files to preview (1-5 files)"),
):
    """
    Upload files for preview and duplicate detection.
    Returns preview data including column information and duplicate groups.
    """
    try:
        # Validate file count
        if len(files) < 1:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "INVALID_FILE_COUNT",
                        "message": "At least 1 file is required",
                        "details": {"min_files": 1}
                    }
                }
            )

        if len(files) > config.MAX_FILES_PER_JOB:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "TOO_MANY_FILES",
                        "message": f"Maximum {config.MAX_FILES_PER_JOB} files allowed",
                        "details": {
                            "max_files": config.MAX_FILES_PER_JOB,
                            "provided": len(files)
                        }
                    }
                }
            )

        # Generate preview ID
        preview_id = generate_preview_id()

        # Create preview session FIRST (for foreign key constraint)
        preview_store.create_preview_session(
            preview_id=preview_id,
            file_count=len(files),
            total_duplicates_detected=0  # Will update later
        )

        # Import required modules
        from src.excel_loader import ExcelLoader
        from src.duplicate_detector import DuplicateDetector
        from src.utils.data_types import DataTypeInferrer
        import pandas as pd

        detector = DuplicateDetector(enable_llm=True)

        file_previews = []
        total_duplicates = 0

        # Process each file
        for file in files:
            # Create fresh loader for each file to avoid state sharing
            loader = ExcelLoader()
            # Read file content
            content = await file.read()
            file_size = len(content)

            # Validate file
            is_valid, error_msg = file_store.validate_file(file.filename, file_size)
            if not is_valid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "code": "INVALID_FILE",
                            "message": error_msg,
                            "details": {"filename": file.filename}
                        }
                    }
                )

            # Load file into DataFrame (sample first 10,000 rows)
            try:
                from io import BytesIO
                import tempfile

                # Save to temp file (close it before pandas reads it on Windows)
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                # File is now closed, safe to read with pandas

                # Load DataFrame
                loaded_files = loader.load_files([tmp_path])
                df = list(loaded_files.values())[0]

                # Sample first 10,000 rows (make a copy to allow cleanup)
                df_sample = df.head(10000).copy()

                # Clean up temp file with retry logic for Windows
                import os
                import time
                if os.path.exists(tmp_path):
                    # Delete references to allow file cleanup
                    del loaded_files

                    for attempt in range(3):
                        try:
                            os.unlink(tmp_path)
                            break
                        except (PermissionError, OSError):
                            if attempt < 2:
                                import gc
                                gc.collect()  # Force garbage collection
                                time.sleep(0.1)  # Wait 100ms and retry
                            else:
                                logger.warning(f"Could not delete temp file {tmp_path}, it will be cleaned up later")

            except Exception as e:
                logger.error(f"Failed to load file {file.filename}: {e}")
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "code": "FILE_LOAD_ERROR",
                            "message": f"Failed to load file: {str(e)}",
                            "details": {"filename": file.filename}
                        }
                    }
                )

            # Detect duplicates
            duplicate_groups = detector.detect_duplicates(df_sample)
            total_duplicates += len(duplicate_groups)

            # Create column previews
            column_previews = []
            for col in df_sample.columns:
                non_null = df_sample[col].dropna()

                # Convert sample values to JSON-serializable format
                sample_values = non_null.head(5).tolist()
                # Handle pandas Timestamp and other non-serializable types
                sample_values = [
                    str(val) if hasattr(val, 'isoformat') or isinstance(val, (pd.Timestamp, datetime))
                    else val
                    for val in sample_values
                ]

                column_preview = ColumnPreview(
                    name=col,
                    data_type=DataTypeInferrer.infer_type(df_sample[col]),
                    null_count=int(df_sample[col].isna().sum()),
                    unique_count=int(df_sample[col].nunique()),
                    sample_values=sample_values
                )
                column_previews.append(column_preview)

            # Convert duplicate groups to DuplicateGroupInfo
            duplicate_infos = []
            for group in duplicate_groups:
                duplicate_info = DuplicateGroupInfo(
                    group_id=group.group_id,
                    detection_type=group.detection_type,
                    similarity_score=group.similarity_score,
                    columns=group.columns,
                    metadata={
                        "content_identical": group.content_identical,
                        "sample_comparison": group.sample_comparison,
                        **group.metadata
                    },
                    recommendation=group.recommendation
                )
                duplicate_infos.append(duplicate_info)

            # Save preview file with DataFrame and metadata
            metadata = {
                "columns": [cp.dict() for cp in column_previews],
                "duplicate_groups": [di.dict() for di in duplicate_infos]
            }

            preview_file = preview_store.save_preview_file(
                preview_id=preview_id,
                original_filename=file.filename,
                file_content=content,
                df=df,  # Save full DataFrame, not just sample
                metadata=metadata
            )

            # Create file preview response
            file_preview = FilePreview(
                file_id=preview_file.file_id,
                original_filename=preview_file.original_filename,
                row_count=preview_file.row_count,
                column_count=preview_file.column_count,
                columns=column_previews,
                duplicate_groups=duplicate_infos
            )
            file_previews.append(file_preview)

        # Update preview session with total duplicates count
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE preview_sessions SET total_duplicates_detected = ? WHERE preview_id = ?",
                (total_duplicates, preview_id)
            )

        logger.info(f"Created preview {preview_id} with {len(files)} files, {total_duplicates} duplicate groups")

        return JobPreviewResponse(
            preview_id=preview_id,
            status="preview_ready",
            created_at=datetime.utcnow(),
            file_count=len(files),
            files=file_previews,
            total_duplicates_detected=total_duplicates,
            message="Preview ready. Review duplicate columns before processing."
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create preview: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "PREVIEW_CREATION_FAILED",
                    "message": "Failed to create preview",
                    "details": str(e)
                }
            }
        )


@router.post("/preview/{preview_id}/confirm", response_model=JobConfirmResponse, status_code=201)
async def confirm_preview(
    preview_id: str,
    request: JobConfirmRequest,
    background_tasks: BackgroundTasks = None
):
    """
    Confirm preview and start processing with selected columns deleted.
    """
    try:
        # Get preview session
        session = preview_store.get_preview_session(preview_id)
        if not session:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "PREVIEW_NOT_FOUND",
                        "message": f"Preview {preview_id} not found",
                        "details": {"preview_id": preview_id}
                    }
                }
            )

        # Load DataFrames
        dataframes = preview_store.load_all_dataframes(preview_id)

        # Get preview files to check for Tableau source
        preview_files = preview_store.get_preview_files(preview_id)
        is_tableau_source = False

        # Create mapping from file_id to full dict key (file_id_original_filename)
        file_id_to_dict_key = {}

        if preview_files:
            # Check first file's metadata for Tableau source
            first_file = preview_files[0]
            if first_file.metadata_json:
                metadata = json.loads(first_file.metadata_json)
                is_tableau_source = metadata.get("source") == "tableau"

            # Build mapping
            for pf in preview_files:
                dict_key = f"{pf.file_id}_{pf.original_filename}"
                file_id_to_dict_key[pf.file_id] = dict_key

        # Apply column deletions
        columns_removed = 0
        file_paths = []

        for selection in request.file_selections:
            # Map file_id to dict key
            dict_key = file_id_to_dict_key.get(selection.file_id)
            if not dict_key or dict_key not in dataframes:
                logger.warning(f"File {selection.file_id} not found in preview")
                continue

            df = dataframes[dict_key]

            # Drop selected columns
            if selection.columns_to_delete:
                cols_to_drop = [c for c in selection.columns_to_delete if c in df.columns]
                df = df.drop(columns=cols_to_drop)
                columns_removed += len(cols_to_drop)
                logger.info(f"Dropped {len(cols_to_drop)} columns from {selection.file_id}")

            # Update dataframe in dict using the full key
            dataframes[dict_key] = df

            # For Excel workflow, save to file
            if not is_tableau_source:
                file_path = preview_store.get_file_path(selection.file_id)
                if file_path:
                    # Overwrite original file with cleaned DataFrame
                    import pandas as pd
                    df.to_excel(file_path, index=False)
                    file_paths.append(file_path)

        # Generate job ID
        job_id = generate_job_id()

        # Create job in database
        file_count = len(dataframes) if is_tableau_source else len(file_paths)
        job = job_store.create_job(job_id=job_id, file_count=file_count)

        # Mark preview as confirmed
        preview_store.update_session_status(preview_id, "confirmed")

        # Schedule background job based on source type
        if is_tableau_source:
            logger.info(f"Scheduling Tableau discovery job for {len(dataframes)} tables")
            background_tasks.add_task(
                execute_tableau_discovery_job,
                job_id=job_id,
                dataframes=dataframes
            )
        else:
            logger.info(f"Scheduling Excel discovery job for {len(file_paths)} files")
            background_tasks.add_task(
                execute_discovery_job,
                job_id=job_id,
                file_paths=file_paths
            )

        # Don't delete preview immediately - let background task handle cleanup
        # or let scheduled cleanup delete it after 1 hour

        logger.info(f"Confirmed preview {preview_id}, created job {job_id}, removed {columns_removed} columns")

        return JobConfirmResponse(
            job_id=job_id,
            status=JobStatus.RUNNING,
            created_at=datetime.utcnow(),
            file_count=file_count,
            columns_removed=columns_removed,
            message="Job created successfully and processing started"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to confirm preview: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "PREVIEW_CONFIRM_FAILED",
                    "message": "Failed to confirm preview",
                    "details": str(e)
                }
            }
        )


@router.delete("/preview/{preview_id}", response_model=PreviewDeleteResponse)
async def cancel_preview(preview_id: str):
    """
    Cancel a preview and delete all associated files.
    """
    try:
        # Check if preview exists
        session = preview_store.get_preview_session(preview_id)
        if not session:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "PREVIEW_NOT_FOUND",
                        "message": f"Preview {preview_id} not found",
                        "details": {"preview_id": preview_id}
                    }
                }
            )

        # Delete preview
        files_deleted = preview_store.delete_preview(preview_id)

        logger.info(f"Cancelled preview {preview_id}")

        return PreviewDeleteResponse(
            message=f"Preview {preview_id} cancelled and files deleted",
            preview_id=preview_id,
            files_deleted=files_deleted
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel preview {preview_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "PREVIEW_DELETE_FAILED",
                    "message": "Failed to cancel preview",
                    "details": str(e)
                }
            }
        )


# Import JSONResponse for 202 status
from fastapi.responses import JSONResponse


@router.delete("/{job_id}/relationships/{relationship_id}")
async def delete_relationship(
    job_id: str,
    relationship_id: str
):
    """
    Delete a specific relationship from job results.

    This is a soft delete - marks the relationship as deleted in the result JSON.
    The relationship will be filtered out from future API responses.

    Args:
        job_id: The job ID
        relationship_id: The relationship ID to delete

    Returns:
        Success response with remaining relationship count
    """
    try:
        # Load job result
        result_data = result_store.get_result(job_id)

        if not result_data:
            logger.error(f"Result not found for job {job_id}")
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "JOB_NOT_FOUND",
                        "message": f"Job {job_id} not found",
                        "details": {"job_id": job_id}
                    }
                }
            )

        # The result structure is flat - relationships are at top level
        # NOT nested under "result" key
        relationships = result_data.get("relationships", [])
        logger.info(f"Found {len(relationships)} total relationships in job {job_id}")
        found = False

        for rel in relationships:
            if rel.get("relationship_id") == relationship_id:
                rel["deleted"] = True
                rel["deleted_at"] = datetime.now().isoformat()
                found = True
                logger.info(f"Marked relationship {relationship_id} as deleted")
                break

        if not found:
            # Log available relationship IDs for debugging
            available_ids = [r.get("relationship_id") for r in relationships]
            logger.error(f"Relationship {relationship_id} not found. Available IDs: {available_ids}")
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "RELATIONSHIP_NOT_FOUND",
                        "message": f"Relationship {relationship_id} not found in job {job_id}",
                        "details": {
                            "job_id": job_id,
                            "relationship_id": relationship_id,
                            "available_relationships": available_ids
                        }
                    }
                }
            )

        # Update result in storage
        result_store.update_result(job_id, result_data)

        # Count remaining active relationships
        active_count = sum(1 for r in relationships if not r.get("deleted", False))

        logger.info(f"Deleted relationship {relationship_id} from job {job_id}. {active_count} relationships remaining.")

        return {
            "success": True,
            "relationship_id": relationship_id,
            "remaining_relationships": active_count,
            "message": "Relationship deleted successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete relationship {relationship_id} from job {job_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "DELETE_RELATIONSHIP_FAILED",
                    "message": "Failed to delete relationship",
                    "details": str(e)
                }
            }
        )


@router.patch("/{job_id}/relationships/{relationship_id}/inclusion")
async def update_relationship_inclusion(
    job_id: str,
    relationship_id: str,
    request: dict
):
    """
    Update the inclusion state of a specific relationship.

    This marks the relationship as included or excluded in the result JSON.
    Excluded relationships will still be stored but can be visually hidden in the UI.

    Args:
        job_id: The job ID
        relationship_id: The relationship ID to update
        request: { "included": true/false }

    Returns:
        Success response with updated inclusion state
    """
    try:
        # Validate request body
        included = request.get("included")
        if included is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "Missing 'included' field in request body",
                        "details": {"expected": "{ \"included\": true/false }"}
                    }
                }
            )

        # Load job result
        result_data = result_store.get_result(job_id)

        if not result_data:
            logger.error(f"Result not found for job {job_id}")
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "JOB_NOT_FOUND",
                        "message": f"Job {job_id} not found",
                        "details": {"job_id": job_id}
                    }
                }
            )

        # The result structure is flat - relationships are at top level
        relationships = result_data.get("relationships", [])
        logger.info(f"Found {len(relationships)} total relationships in job {job_id}")
        found = False

        for rel in relationships:
            if rel.get("relationship_id") == relationship_id:
                rel["excluded"] = not included
                rel["updated_at"] = datetime.now().isoformat()
                found = True
                logger.info(f"Updated relationship {relationship_id} inclusion to {included}")
                break

        if not found:
            # Log available relationship IDs for debugging
            available_ids = [r.get("relationship_id") for r in relationships]
            logger.error(f"Relationship {relationship_id} not found. Available IDs: {available_ids}")
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "RELATIONSHIP_NOT_FOUND",
                        "message": f"Relationship {relationship_id} not found in job {job_id}",
                        "details": {
                            "job_id": job_id,
                            "relationship_id": relationship_id,
                            "available_relationships": available_ids
                        }
                    }
                }
            )

        # Update result in storage
        result_store.update_result(job_id, result_data)

        logger.info(f"Updated relationship {relationship_id} inclusion state in job {job_id} to {included}")

        return {
            "success": True,
            "relationship_id": relationship_id,
            "included": included,
            "message": f"Relationship {'included' if included else 'excluded'} successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update relationship {relationship_id} inclusion in job {job_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "UPDATE_RELATIONSHIP_FAILED",
                    "message": "Failed to update relationship inclusion",
                    "details": str(e)
                }
            }
        )
