import os
import subprocess
import zipfile
from pathlib import Path
from celery import Celery
from pymongo import MongoClient
from datetime import datetime, timezone
import trimesh
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Celery configuration
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://redis:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://redis:6379/0')

celery = Celery('tasks', broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)

# MongoDB connection
MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ['DB_NAME']
client = MongoClient(MONGO_URL)
db = client[DB_NAME]

# Storage paths
STORAGE_BASE_DIR = Path.cwd() / "local_storage"
RAW_UPLOADS_DIR = STORAGE_BASE_DIR / "raw_uploads"
PROCESSED_MODELS_DIR = STORAGE_BASE_DIR / "processed_models"

@celery.task
def process_photogrammetry(job_id: str, menu_item_id: str, zip_filename: str):
    """
    Celery task to perform photogrammetry processing.
    """
    try:
        # Update job status to PROCESSING
        db.photogrammetry_jobs.update_one(
            {"id": job_id},
            {"$set": {"status": "PROCESSING"}}
        )

        # 1. Unzip the images
        zip_path = RAW_UPLOADS_DIR / zip_filename
        unzip_dir = RAW_UPLOADS_DIR / job_id
        unzip_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(unzip_dir)

        # 2. Run Meshroom
        output_dir = PROCESSED_MODELS_DIR / job_id
        output_dir.mkdir(exist_ok=True)

        meshroom_cmd = [
            "meshroom_batch",
            "--input", str(unzip_dir),
            "--output", str(output_dir)
        ]
        subprocess.run(meshroom_cmd, check=True, capture_output=True, text=True)

        # 3. Find the generated model
        obj_files = list(output_dir.glob("**/texturedMesh.obj"))
        if not obj_files:
            raise Exception("Meshroom did not produce an OBJ file.")
        obj_path = obj_files[0]

        # 4. Optimize and convert to GLB using trimesh
        mesh = trimesh.load(obj_path)
        glb_filename = f"{menu_item_id}.glb"
        glb_path = PROCESSED_MODELS_DIR / glb_filename
        mesh.export(glb_path, file_type='glb')

        # 5. Update the database
        model_url = f"/static/models/{glb_filename}"
        db.menu_items.update_one(
            {"id": menu_item_id},
            {"$set": {"model_url": model_url}}
        )

        db.photogrammetry_jobs.update_one(
            {"id": job_id},
            {"$set": {
                "status": "COMPLETED",
                "completed_at": datetime.now(timezone.utc).isoformat()
            }}
        )

    except Exception as e:
        db.photogrammetry_jobs.update_one(
            {"id": job_id},
            {"$set": {
                "status": "FAILED",
                "error_message": str(e),
                "completed_at": datetime.now(timezone.utc).isoformat()
            }}
        )
    finally:
        # Clean up temporary files
        if 'unzip_dir' in locals() and unzip_dir.exists():
            subprocess.run(["rm", "-rf", str(unzip_dir)])
        if 'output_dir' in locals() and output_dir.exists():
            subprocess.run(["rm", "-rf", str(output_dir)])
