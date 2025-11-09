import os
import shutil
from pathlib import Path

# Define the base directory for local storage
STORAGE_BASE_DIR = Path.cwd() / "local_storage"
RAW_UPLOADS_DIR = STORAGE_BASE_DIR / "raw_uploads"
PROCESSED_MODELS_DIR = STORAGE_BASE_DIR / "processed_models"

# Ensure the directories exist
os.makedirs(RAW_UPLOADS_DIR, exist_ok=True)
os.makedirs(PROCESSED_MODELS_DIR, exist_ok=True)


def save_raw_upload(file_content: bytes, filename: str) -> Path:
    """Saves the raw uploaded file to the local storage."""
    filepath = RAW_UPLOADS_DIR / filename
    with open(filepath, "wb") as f:
        f.write(file_content)
    return filepath


def save_processed_model(file_content: bytes, filename: str) -> str:
    """Saves the processed model to the local storage and returns its URL."""
    filepath = PROCESSED_MODELS_DIR / filename
    with open(filepath, "wb") as f:
        f.write(file_content)
    return f"/static/models/{filename}"


def delete_file(filepath: str):
    """Deletes a file from the local storage."""
    if os.path.exists(filepath):
        os.remove(filepath)


def clear_directory(directory: Path):
    """Deletes all files within a directory."""
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print(f'Failed to delete {file_path}. Reason: {e}')
