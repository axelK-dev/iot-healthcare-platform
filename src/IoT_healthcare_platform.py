import os
import json
import logging
import asyncio
import random
import sqlite3
from typing import Dict, Any
from fastapi import FastAPI, WebSocket, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
import jwt

# -----------------------------
# Base Directory and Config Paths
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# -----------------------------
# Utility: Load JSON Config Safely
# -----------------------------
def load_config(file_name):
    file_path = os.path.join(BASE_DIR, file_name)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.error(f"Invalid JSON in {file_name}. Using empty config.")
            return {}
    else:
        logging.warning(f"{file_name} not found. Using empty config.")
        return {}

# -----------------------------
# Load Configurations
# -----------------------------
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'r') as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError:
            logging.error("Invalid JSON in config.json. Using defaults.")
            config = {}
else:
    logging.warning("config.json not found. Using defaults.")
    config = {}

SECRET_KEY = config.get("SECRET_KEY", "defaultsecret")
ROLES = config.get("ROLES", ["admin", "provider", "patient"])
ALGORITHM = "HS256"

architecture = load_config("IoT_healthcare_platform_architecture.json")
security = load_config("IoT_healthcare_platform_security.json")
workflows = load_config("IoT_healthcare_platform_workflows.json")

# -----------------------------
# Logging Configuration
# -----------------------------
logging.basicConfig(
    filename='iot_healthcare_api.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# -----------------------------
# Database Setup (SQLite)
# -----------------------------
DB_FILE = os.path.join(BASE_DIR, "iot_devices.db")
conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT UNIQUE,
    status TEXT,
    trust_chain BOOLEAN
)
""")
conn.commit()

# -----------------------------
# FastAPI App
# -----------------------------
app = FastAPI(title="IoT Healthcare Platform API", version="1.1")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# -----------------------------
# JWT Utility Functions
# -----------------------------
def create_token(username: str, role: str):
    payload = {"sub": username, "role": role}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_current_user(token: str = Depends(oauth2_scheme)):
    return verify_token(token)

# -----------------------------
# IoT Provisioning with DB Persistence
# -----------------------------
class IoTProvisioning:
    def onboard_device(self, device_id: str, voucher: str):
        if not voucher.startswith("VOUCHER-"):
            raise ValueError("Invalid voucher format.")
        try:
            cursor.execute("INSERT INTO devices (device_id, status, trust_chain) VALUES (?, ?, ?)",
                           (device_id, "Provisioned", True))
            conn.commit()
            logging.info(f"Device {device_id} onboarded successfully.")
            return {"device_id": device_id, "status": "Provisioned", "trust_chain": True}
        except sqlite3.IntegrityError:
            raise ValueError("Device already exists.")

# -----------------------------
# Compliance Validation
# -----------------------------
class ComplianceValidator:
    def __init__(self, rules: Dict[str, Any]):
        self.rules = rules

    def validate(self, workflow: Dict[str, Any]):
        for rule, expected in self.rules.items():
            if workflow.get(rule) != expected:
                logging.warning(f"Compliance failed for {rule}.")
                return False
        logging.info("Workflow passed compliance checks.")
        return True

# -----------------------------
# Monitoring Service (Async)
# -----------------------------
async def generate_sensor_data():
    while True:
        yield {
            "heart_rate": random.randint(60, 130),
            "temperature": round(random.uniform(36.0, 39.0), 1),
            "spo2": random.randint(90, 100)
        }
        await asyncio.sleep(3)

# -----------------------------
# Request Models
# -----------------------------
class LoginRequest(BaseModel):
    username: str
    role: str

class OnboardingRequest(BaseModel):
    device_id: str
    voucher: str

class ComplianceRequest(BaseModel):
    workflow: Dict[str, Any]

# -----------------------------
# API Endpoints
# -----------------------------
@app.post("/login")
async def login(request: LoginRequest):
    if request.role not in ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    token = create_token(request.username, request.role)
    return {"access_token": token, "token_type": "bearer"}

@app.get("/health")
async def health_check():
    return {"status": "API is running", "version": "1.1"}

@app.post("/onboard")
async def onboard_device(request: OnboardingRequest, user: dict = Depends(get_current_user)):
    if user.get("role") not in ["admin", "provider"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    provisioning = IoTProvisioning()
    try:
        device = provisioning.onboard_device(request.device_id, request.voucher)
        return {"message": "Device onboarded successfully", "device": device}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/compliance")
async def validate_compliance(request: ComplianceRequest, user: dict = Depends(get_current_user)):
    if user.get("role") not in ["admin", "provider"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    compliance_rules = {"HIPAA": True, "GDPR": True}
    validator = ComplianceValidator(compliance_rules)
    result = validator.validate(request.workflow)
    return {"compliance_passed": result}

@app.get("/architecture")
async def get_architecture():
    return architecture

@app.get("/security")
async def get_security():
    return security

@app.get("/workflows")
async def get_workflows():
    return workflows

@app.websocket("/monitor")
async def monitor_data(websocket: WebSocket):
    await websocket.accept()
    async for data in generate_sensor_data():
        await websocket.send_json(data)

# -----------------------------
# Main Entry Point
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("IoT_healthcare_platform:app", host="127.0.0.1", port=8000, reload=True)