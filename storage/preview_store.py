"""
Preview session storage management.
Manages preview sessions, file metadata, and DataFrame persistence using pickle.
"""

import os
import json
import pickle
import shutil
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
from loguru import logger
import pandas as pd

from storage.database import get_db_connection
from api.config import config


class PreviewFile:
    """Preview file metadata"""
    def __init__(
        self,
        file_id: str,
        preview_id: str,
        original_filename: str,
        file_path: str,
        dataframe_pickle_path: Optional[str] = None,
        row_count: Optional[int] = None,
        column_count: Optional[int] = None,
        metadata_json: Optional[str] = None
    ):
        self.file_id = file_id
        self.preview_id = preview_id
        self.original_filename = original_filename
        self.file_path = file_path
        self.dataframe_pickle_path = dataframe_pickle_path
        self.row_count = row_count
        self.column_count = column_count
        self.metadata_json = metadata_json


class PreviewSession:
    """Preview session metadata"""
    def __init__(
        self,
        preview_id: str,
        status: str,
        created_at: datetime,
        file_count: int,
        total_duplicates_detected: int = 0
    ):
        self.preview_id = preview_id
        self.status = status
        self.created_at = created_at
        self.file_count = file_count
        self.total_duplicates_detected = total_duplicates_detected


class PreviewStore:
    """Manages preview session storage and DataFrame persistence"""

    def __init__(self):
        # Ensure preview directory exists
        self.preview_base_dir = Path(config.UPLOAD_DIR) / "previews"
        self.preview_base_dir.mkdir(parents=True, exist_ok=True)

    def create_preview_session(
        self,
        preview_id: str,
        file_count: int,
        total_duplicates_detected: int = 0
    ) -> PreviewSession:
        """
        Create a new preview session.

        Args:
            preview_id: Unique preview identifier
            file_count: Number of files in preview
            total_duplicates_detected: Total duplicate groups detected

        Returns:
            PreviewSession object
        """
        session = PreviewSession(
            preview_id=preview_id,
            status="preview_ready",
            created_at=datetime.utcnow(),
            file_count=file_count,
            total_duplicates_detected=total_duplicates_detected
        )

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO preview_sessions
                (preview_id, status, created_at, file_count, total_duplicates_detected)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session.preview_id,
                    session.status,
                    session.created_at,
                    session.file_count,
                    session.total_duplicates_detected
                )
            )

        # Create preview directory
        preview_dir = self.preview_base_dir / preview_id
        preview_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Created preview session {preview_id} with {file_count} files")
        return session

    def save_preview_file(
        self,
        preview_id: str,
        original_filename: str,
        file_content: bytes,
        df: pd.DataFrame,
        metadata: Dict[str, Any]
    ) -> PreviewFile:
        """
        Save a preview file with its DataFrame and metadata.

        Args:
            preview_id: Preview session identifier
            original_filename: Original file name
            file_content: Raw file content
            df: Loaded DataFrame
            metadata: Preview metadata (columns, duplicates, etc.)

        Returns:
            PreviewFile object
        """
        # Generate unique file ID
        file_id = f"file_{uuid.uuid4().hex[:12]}"

        # Create preview directory
        preview_dir = self.preview_base_dir / preview_id
        preview_dir.mkdir(parents=True, exist_ok=True)

        # Save raw file
        file_extension = Path(original_filename).suffix
        stored_filename = f"{file_id}_{original_filename}"
        file_path = preview_dir / stored_filename

        with open(file_path, 'wb') as f:
            f.write(file_content)

        # Save DataFrame as pickle
        pickle_filename = f"{file_id}.pkl"
        pickle_path = preview_dir / pickle_filename

        with open(pickle_path, 'wb') as f:
            pickle.dump(df, f)

        # Store metadata
        preview_file = PreviewFile(
            file_id=file_id,
            preview_id=preview_id,
            original_filename=original_filename,
            file_path=str(file_path),
            dataframe_pickle_path=str(pickle_path),
            row_count=len(df),
            column_count=len(df.columns),
            metadata_json=json.dumps(metadata)
        )

        # Save to database
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO preview_files
                (file_id, preview_id, original_filename, file_path, dataframe_pickle_path,
                 row_count, column_count, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preview_file.file_id,
                    preview_file.preview_id,
                    preview_file.original_filename,
                    preview_file.file_path,
                    preview_file.dataframe_pickle_path,
                    preview_file.row_count,
                    preview_file.column_count,
                    preview_file.metadata_json
                )
            )

        logger.info(f"Saved preview file {original_filename} ({len(df)} rows, {len(df.columns)} cols)")
        return preview_file

    def load_dataframe(self, file_id: str) -> Optional[pd.DataFrame]:
        """
        Load a DataFrame from pickle.

        Args:
            file_id: File identifier

        Returns:
            DataFrame or None if not found
        """
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT dataframe_pickle_path FROM preview_files WHERE file_id = ?",
                (file_id,)
            ).fetchone()

        if not row or not row["dataframe_pickle_path"]:
            logger.warning(f"No pickle file found for {file_id}")
            return None

        pickle_path = Path(row["dataframe_pickle_path"])

        if not pickle_path.exists():
            logger.warning(f"Pickle file does not exist: {pickle_path}")
            return None

        try:
            with open(pickle_path, 'rb') as f:
                df = pickle.load(f)
            logger.debug(f"Loaded DataFrame from {pickle_path}")
            return df
        except Exception as e:
            logger.error(f"Failed to load pickle {pickle_path}: {e}")
            return None

    def load_all_dataframes(self, preview_id: str) -> Dict[str, pd.DataFrame]:
        """
        Load all DataFrames for a preview session.

        Args:
            preview_id: Preview session identifier

        Returns:
            Dict mapping file_id_original_filename to DataFrame
        """
        dataframes = {}

        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT file_id, original_filename, dataframe_pickle_path FROM preview_files WHERE preview_id = ?",
                (preview_id,)
            ).fetchall()

        for row in rows:
            file_id = row["file_id"]
            original_filename = row["original_filename"]
            df = self.load_dataframe(file_id)
            if df is not None:
                # Use file_id_original_filename as key to match stored file pattern
                dict_key = f"{file_id}_{original_filename}"
                dataframes[dict_key] = df

        logger.info(f"Loaded {len(dataframes)} DataFrames for preview {preview_id}")
        return dataframes

    def get_preview_session(self, preview_id: str) -> Optional[PreviewSession]:
        """
        Get preview session metadata.

        Args:
            preview_id: Preview identifier

        Returns:
            PreviewSession or None
        """
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT preview_id, status, created_at, file_count, total_duplicates_detected
                FROM preview_sessions
                WHERE preview_id = ?
                """,
                (preview_id,)
            ).fetchone()

        if not row:
            return None

        return PreviewSession(
            preview_id=row["preview_id"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            file_count=row["file_count"],
            total_duplicates_detected=row["total_duplicates_detected"]
        )

    def get_preview_files(self, preview_id: str) -> List[PreviewFile]:
        """
        Get all files for a preview session.

        Args:
            preview_id: Preview identifier

        Returns:
            List of PreviewFile objects
        """
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT file_id, preview_id, original_filename, file_path,
                       dataframe_pickle_path, row_count, column_count, metadata_json
                FROM preview_files
                WHERE preview_id = ?
                """,
                (preview_id,)
            ).fetchall()

        files = []
        for row in rows:
            files.append(PreviewFile(
                file_id=row["file_id"],
                preview_id=row["preview_id"],
                original_filename=row["original_filename"],
                file_path=row["file_path"],
                dataframe_pickle_path=row["dataframe_pickle_path"],
                row_count=row["row_count"],
                column_count=row["column_count"],
                metadata_json=row["metadata_json"]
            ))

        return files

    def get_file_metadata(self, file_id: str) -> Optional[Dict[str, Any]]:
        """
        Get file preview metadata (columns, duplicates, etc.).

        Args:
            file_id: File identifier

        Returns:
            Metadata dict or None
        """
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM preview_files WHERE file_id = ?",
                (file_id,)
            ).fetchone()

        if not row or not row["metadata_json"]:
            return None

        try:
            return json.loads(row["metadata_json"])
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse metadata for {file_id}: {e}")
            return None

    def update_session_status(self, preview_id: str, status: str):
        """
        Update preview session status.

        Args:
            preview_id: Preview identifier
            status: New status ('preview_ready', 'confirmed', 'cancelled')
        """
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE preview_sessions SET status = ? WHERE preview_id = ?",
                (status, preview_id)
            )

        logger.info(f"Updated preview {preview_id} status to {status}")

    def delete_preview(self, preview_id: str) -> int:
        """
        Delete a preview session and all associated files.

        Args:
            preview_id: Preview identifier

        Returns:
            Number of files deleted
        """
        # Get preview directory
        preview_dir = self.preview_base_dir / preview_id

        # Count files
        files_deleted = 0

        # Delete directory
        if preview_dir.exists():
            shutil.rmtree(preview_dir)
            # Count files in directory before deletion
            files_deleted = len(list(preview_dir.glob("*")))

        # Delete from database (cascade will delete preview_files)
        with get_db_connection() as conn:
            conn.execute(
                "DELETE FROM preview_sessions WHERE preview_id = ?",
                (preview_id,)
            )

        logger.info(f"Deleted preview session {preview_id} ({files_deleted} files)")
        return files_deleted

    def cleanup_expired_previews(self, hours: int = 1) -> int:
        """
        Delete preview sessions older than specified hours.

        Args:
            hours: Number of hours to keep previews

        Returns:
            Number of previews deleted
        """
        with get_db_connection() as conn:
            # Get expired preview IDs
            rows = conn.execute(
                """
                SELECT preview_id FROM preview_sessions
                WHERE created_at < datetime('now', '-' || ? || ' hours')
                """,
                (hours,)
            ).fetchall()

        expired_ids = [row["preview_id"] for row in rows]

        # Delete each preview
        deleted_count = 0
        for preview_id in expired_ids:
            try:
                self.delete_preview(preview_id)
                deleted_count += 1
            except Exception as e:
                logger.error(f"Failed to delete expired preview {preview_id}: {e}")

        logger.info(f"Cleaned up {deleted_count} expired preview sessions")
        return deleted_count

    def get_file_path(self, file_id: str) -> Optional[str]:
        """
        Get the file path for a preview file.

        Args:
            file_id: File identifier

        Returns:
            File path or None
        """
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT file_path FROM preview_files WHERE file_id = ?",
                (file_id,)
            ).fetchone()

        return row["file_path"] if row else None
