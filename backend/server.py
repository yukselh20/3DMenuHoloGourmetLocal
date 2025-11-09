from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
from passlib.context import CryptContext
from jose import JWTError, jwt
import zipfile
import io
from fastapi.staticfiles import StaticFiles
import storage_utils
from celery import Celery

# Celery configuration
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://redis:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://redis:6379/0')

celery = Celery('tasks', broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)


ROOT_DIR = Path(__file__).parent

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT Configuration
SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'your-secret-key-change-in-production')
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Create the main app
app = FastAPI()
api_router = APIRouter(prefix="/api")
security = HTTPBearer()

# Mount static files directory
app.mount("/static/models", StaticFiles(directory="local_storage/processed_models"), name="models")

# Models
class User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: EmailStr
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class UserCreate(BaseModel):
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class PhotogrammetryJob(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    menu_item_id: str
    status: str  # PENDING, PROCESSING, COMPLETED, FAILED
    progress: Optional[float] = 0.0
    current_stage: Optional[str] = "PENDING"
    raw_images_zip_path: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

class MenuItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    price: float
    allergens: Optional[List[str]] = None
    dimensions_cm: dict  # {"diameter": 28, "height": 10}
    model_url: Optional[str] = None
    owner_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latest_job: Optional[PhotogrammetryJob] = None

class MenuItemCreate(BaseModel):
    name: str
    description: str
    price: float
    allergens: Optional[List[str]] = None
    dimensions_cm: dict

class MenuItemUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    allergens: Optional[List[str]] = None
    dimensions_cm: Optional[dict] = None

# Helper functions
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    if user is None:
        raise credentials_exception
    
    # Convert ISO string to datetime if needed
    if isinstance(user.get('created_at'), str):
        user['created_at'] = datetime.fromisoformat(user['created_at'])
    
    return User(**user)

# Auth Routes
@api_router.post("/auth/register", response_model=Token)
async def register(user_create: UserCreate):
    # Check if user already exists
    existing_user = await db.users.find_one({"email": user_create.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Create new user
    user = User(
        email=user_create.email
    )
    
    # Store user with hashed password
    user_doc = user.model_dump()
    user_doc['created_at'] = user_doc['created_at'].isoformat()
    user_doc['hashed_password'] = get_password_hash(user_create.password)
    
    await db.users.insert_one(user_doc)
    
    # Create access token
    access_token = create_access_token(data={"sub": user.id})
    return Token(access_token=access_token, token_type="bearer")

@api_router.post("/auth/login", response_model=Token)
async def login(user_login: UserLogin):
    # Find user
    user_doc = await db.users.find_one({"email": user_login.email}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Verify password
    if not verify_password(user_login.password, user_doc.get('hashed_password', '')):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Create access token
    access_token = create_access_token(data={"sub": user_doc['id']})
    return Token(access_token=access_token, token_type="bearer")

@api_router.get("/auth/me", response_model=User)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user

# Menu Item Routes
@api_router.post("/menu-items", response_model=MenuItem)
async def create_menu_item(
    menu_item: MenuItemCreate,
    current_user: User = Depends(get_current_user)
):
    item = MenuItem(
        **menu_item.model_dump(),
        owner_id=current_user.id
    )
    
    item_doc = item.model_dump()
    item_doc['created_at'] = item_doc['created_at'].isoformat()
    
    await db.menu_items.insert_one(item_doc)
    return item

@api_router.get("/menu-items", response_model=List[MenuItem])
async def get_menu_items(current_user: User = Depends(get_current_user)):
    items_cursor = db.menu_items.find({"owner_id": current_user.id}, {"_id": 0})
    items = await items_cursor.to_list(1000)
    
    for item in items:
        # Fetch the latest job for each menu item
        latest_job = await db.photogrammetry_jobs.find_one(
            {"menu_item_id": item["id"]},
            {"_id": 0},
            sort=[("created_at", -1)]
        )
        item["latest_job"] = latest_job

        # Convert date strings to datetime objects
        if isinstance(item.get('created_at'), str):
            item['created_at'] = datetime.fromisoformat(item['created_at'])
        if latest_job and isinstance(latest_job.get('created_at'), str):
            latest_job['created_at'] = datetime.fromisoformat(latest_job['created_at'])
        if latest_job and latest_job.get('completed_at') and isinstance(latest_job['completed_at'], str):
            latest_job['completed_at'] = datetime.fromisoformat(latest_job['completed_at'])

    return [MenuItem(**item) for item in items]

@api_router.get("/menu-items/{item_id}", response_model=MenuItem)
async def get_menu_item(
    item_id: str,
    current_user: User = Depends(get_current_user)
):
    item = await db.menu_items.find_one(
        {"id": item_id, "owner_id": current_user.id},
        {"_id": 0}
    )
    
    if not item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    
    if isinstance(item.get('created_at'), str):
        item['created_at'] = datetime.fromisoformat(item['created_at'])
    
    return MenuItem(**item)

@api_router.put("/menu-items/{item_id}", response_model=MenuItem)
async def update_menu_item(
    item_id: str,
    menu_item_update: MenuItemUpdate,
    current_user: User = Depends(get_current_user)
):
    # Check if item exists and belongs to user
    existing_item = await db.menu_items.find_one(
        {"id": item_id, "owner_id": current_user.id}
    )
    
    if not existing_item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    
    # Update only provided fields
    update_data = {k: v for k, v in menu_item_update.model_dump().items() if v is not None}
    
    if update_data:
        await db.menu_items.update_one(
            {"id": item_id},
            {"$set": update_data}
        )
    
    # Fetch and return updated item
    updated_item = await db.menu_items.find_one({"id": item_id}, {"_id": 0})
    
    if isinstance(updated_item.get('created_at'), str):
        updated_item['created_at'] = datetime.fromisoformat(updated_item['created_at'])
    
    return MenuItem(**updated_item)

@api_router.delete("/menu-items/{item_id}")
async def delete_menu_item(
    item_id: str,
    current_user: User = Depends(get_current_user)
):
    # Check if item exists and belongs to user
    existing_item = await db.menu_items.find_one(
        {"id": item_id, "owner_id": current_user.id}
    )
    
    if not existing_item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    
    # Delete associated jobs
    await db.photogrammetry_jobs.delete_many({"menu_item_id": item_id})
    
    # Delete menu item
    await db.menu_items.delete_one({"id": item_id})
    
    return {"message": "Menu item deleted successfully"}

@api_router.post("/menu-items/{item_id}/upload-images")
async def upload_images(
    item_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    # Check if item exists and belongs to user
    existing_item = await db.menu_items.find_one(
        {"id": item_id, "owner_id": current_user.id}
    )
    
    if not existing_item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    
    # Validate file is a zip
    if not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="File must be a zip archive")
    
    # Read file content
    file_content = await file.read()
    
    # Validate it's a valid zip file
    try:
        with zipfile.ZipFile(io.BytesIO(file_content)) as zip_file:
            # Check if zip contains image files
            image_extensions = ('.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG')
            image_files = [f for f in zip_file.namelist() if f.lower().endswith(image_extensions)]
            
            if len(image_files) < 5:
                raise HTTPException(
                    status_code=400,
                    detail="Zip file must contain at least 5 images"
                )
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")
    
    # Create job ID
    job_id = str(uuid.uuid4())
    
    # Save the uploaded file to local storage
    zip_filename = f"{job_id}.zip"
    zip_path = storage_utils.save_raw_upload(file_content, zip_filename)
    
    # Create photogrammetry job
    job = PhotogrammetryJob(
        id=job_id,
        menu_item_id=item_id,
        status="PENDING",
        raw_images_zip_path=str(zip_path)
    )
    
    job_doc = job.model_dump()
    job_doc['created_at'] = job_doc['created_at'].isoformat()
    
    await db.photogrammetry_jobs.insert_one(job_doc)
    
    # Send task to Celery worker
    celery.send_task(
        "tasks.process_photogrammetry",
        args=[job_id, item_id, zip_filename]
    )
    
    return {
        "message": "Upload successful",
        "job_id": job_id,
        "status": "PENDING"
    }

# Job Status Routes
@api_router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    current_user: User = Depends(get_current_user)
):
    # Get the job by its ID
    job = await db.photogrammetry_jobs.find_one(
        {"id": job_id},
        {"_id": 0}
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check if the menu item associated with the job belongs to the current user
    menu_item = await db.menu_items.find_one(
        {"id": job["menu_item_id"], "owner_id": current_user.id}
    )

    if not menu_item:
        raise HTTPException(status_code=403, detail="Not authorized to view this job's status")

    # Convert dates if needed
    if isinstance(job.get('created_at'), str):
        job['created_at'] = datetime.fromisoformat(job['created_at'])
    if job.get('completed_at') and isinstance(job['completed_at'], str):
        job['completed_at'] = datetime.fromisoformat(job['completed_at'])

    return PhotogrammetryJob(**job)

# Public Routes
@api_router.get("/public/menu-item/{item_id}")
async def get_public_menu_item(item_id: str):
    item = await db.menu_items.find_one({"id": item_id}, {"_id": 0, "owner_id": 0})
    
    if not item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    
    if isinstance(item.get('created_at'), str):
        item['created_at'] = datetime.fromisoformat(item['created_at'])
    
    return item

@api_router.get("/")
async def root():
    return {"message": "3D/AR Restaurant Menu API"}

# Include router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
