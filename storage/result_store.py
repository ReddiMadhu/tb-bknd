"""Result storage management for analysis reports"""
import json
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger

from api.config import config


class ResultStore:
    """Manages analysis result storage"""

    def __init__(self):
        # Ensure result directory exists
        Path(config.RESULT_DIR).mkdir(parents=True, exist_ok=True)

    def save_result(self, job_id: str, result: Dict[str, Any]) -> str:
        """
        Save analysis result

        Args:
            job_id: Job identifier
            result: Analysis result dictionary

        Returns:
            Path to saved result file
        """
        # Create job-specific directory
        job_dir = Path(config.RESULT_DIR) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        # Save result as JSON
        result_file = job_dir / "report.json"
        with open(result_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved result for job {job_id} to {result_file}")
        return str(result_file)

    def get_result(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get analysis result

        Args:
            job_id: Job identifier

        Returns:
            Analysis result dictionary or None if not found
        """
        result_file = Path(config.RESULT_DIR) / job_id / "report.json"

        if not result_file.exists():
            logger.warning(f"Result file not found for job {job_id}")
            return None

        try:
            with open(result_file, 'r', encoding='utf-8') as f:
                result = json.load(f)
            return result
        except Exception as e:
            logger.error(f"Failed to load result for job {job_id}: {e}")
            return None

    def delete_result(self, job_id: str) -> bool:
        """
        Delete analysis result

        Args:
            job_id: Job identifier

        Returns:
            True if deleted
        """
        job_dir = Path(config.RESULT_DIR) / job_id

        if not job_dir.exists():
            return False

        try:
            import shutil
            shutil.rmtree(job_dir)
            logger.info(f"Deleted result directory for job {job_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete result for job {job_id}: {e}")
            return False

    def result_exists(self, job_id: str) -> bool:
        """
        Check if result exists

        Args:
            job_id: Job identifier

        Returns:
            True if result exists
        """
        result_file = Path(config.RESULT_DIR) / job_id / "report.json"
        return result_file.exists()

    def update_result(self, job_id: str, result: Dict[str, Any]) -> bool:
        """
        Update existing analysis result

        Args:
            job_id: Job identifier
            result: Updated analysis result dictionary

        Returns:
            True if updated successfully
        """
        result_file = Path(config.RESULT_DIR) / job_id / "report.json"

        if not result_file.exists():
            logger.warning(f"Result file not found for job {job_id}, cannot update")
            return False

        try:
            with open(result_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            logger.info(f"Updated result for job {job_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to update result for job {job_id}: {e}")
            return False


# Global result store instance
result_store = ResultStore()
