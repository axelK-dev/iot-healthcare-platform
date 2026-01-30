# Features: RS256 JWT, HSTS, RBAC (deny-by-default), Tamper-Evident Audit, .env support

import os, json, logging, asyncio, random, sqlite3, hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List

from fastapi import FastAPI, WebSocket, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import jwt

# -------------------- .env Loader --------------------
ENV_PATH = Path(".env")
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# -------------------- Config --------------------
BASE_DIR = os.getcwd()
CONFIG_DIR = os.path.join(BASE_DIR, "config")
os.makedirs(CONFIG_DIR, exist_ok=True)

def load_json(path: str) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

SECURITY_FILE   = os.path.join(CONFIG_DIR, "IoT_healthcare_platform_security.json")
WORKFLOWS_FILE  = os.path.join(CONFIG_DIR, "IoT_healthcare_platform_workflows.json")
ARCH_FILE       = os.path.join(CONFIG_DIR, "IoT_healthcare_platform_architecture.json")

security   = load_json(SECURITY_FILE)
workflows  = load_json(WORKFLOWS_FILE)
architecture = load_json(ARCH_FILE)

# -------------------- Keys & JWT (RS256 mandated) --------------------
ALGORITHM = "RS256"
PRIVATE_KEY_PATH = Path(os.getenv("JWT_PRIVATE_KEY", os.path.join(CONFIG_DIR, "jwt_private.pem")))
PUBLIC_KEY_PATH  = Path(os.getenv("JWT_PUBLIC_KEY",  os.path.join(CONFIG_DIR, "jwt_public.pem")))

if not PRIVATE_KEY_PATH.exists() or not PUBLIC_KEY_PATH.exists():
    raise RuntimeError("RSA keys are required. Place jwt_private.pem & jwt_public.pem in config/ or set JWT_PRIVATE_KEY/JWT_PUBLIC_KEY in .env")

PRIVATE_KEY = PRIVATE_KEY_PATH.read_bytes()
PUBLIC_KEY  = PUBLIC_KEY_PATH.read_bytes()

REQUIRED_CLAIMS = set(security.get("jwt", {}).get("require_claims", ["sub","role","iat","exp","iss","aud"]))
ISSUER  = security.get("jwt", {}).get("issuer",   "com.example.iothealth")
AUDIENCE= security.get("jwt", {}).get("audience", "iothealth_clients")
ACCESS_TTL_MIN = int(security.get("jwt", {}).get("access_token_ttl_minutes", 30))

# -------------------- Logging & Tamper-Evident Audit --------------------
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
AUDIT_LOG   = os.path.join(LOG_DIR, "audit.log")
AUDIT_CHAIN = os.path.join(LOG_DIR, "audit.chain")

def _last_hash() -> str:
    try:
        with open(AUDIT_CHAIN, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def audit(event: str, actor: str, resource: str, outcome: str, reason: str = ""):
    if not security.get("audit", {}).get("enabled", True):
        return
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "event": event,
        "actor": actor,
        "resource": resource,
        "outcome": outcome,
        "reason": reason,
    }
    serialized = json.dumps(entry, separators=(",", ":"), sort_keys=True)
    prev = _last_hash().encode("utf-8")
    current_hash = hashlib.sha256(prev + serialized.encode("utf-8")).hexdigest()
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(serialized + "\n")
    with open(AUDIT_CHAIN, "w", encoding="utf-8") as f:
        f.write(current_hash)

# -------------------- Database --------------------
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "iot_devices.db")
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT UNIQUE, status TEXT, trust_chain BOOLEAN)""")
cur.execute("""CREATE TABLE IF NOT EXISTS consents (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, policy_version TEXT, consent_given BOOLEAN, timestamp TEXT)""")
conn.commit()

# -------------------- FastAPI App --------------------
app = FastAPI(
    title="IoT Healthcare Platform API",
    version="2.0",
    description="Mandated security: RS256 JWT, HSTS, RBAC (deny-by-default), tamper-evident audit",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# -------------------- HTTPS Enforcement & Security Headers --------------------
@app.middleware("http")
async def https_enforcer(request: Request, call_next):
    require_https = security.get("transport", {}).get("require_https", True)
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    if require_https and scheme != "https":
        return JSONResponse(status_code=400, content={"error": "HTTPS required by policy"})
    response = await call_next(request)
    if security.get("transport", {}).get("hsts", True):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

# -------------------- RBAC --------------------
def has_permission(role: str, action: str) -> bool:
    rbac = security.get("rbac", {})
    allowed_actions = rbac.get("roles", {}).get(role, [])
    allowed = action in allowed_actions
    if not allowed:
        audit("rbac.deny", role, action, "fail", reason="action_not_permitted")
    return allowed

# -------------------- JWT --------------------
def create_token(username: str, role: str) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ACCESS_TTL_MIN)).timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    if not REQUIRED_CLAIMS.issubset(payload.keys()):
        missing = REQUIRED_CLAIMS - set(payload.keys())
        raise HTTPException(status_code=500, detail=f"Missing required claims: {missing}")
    return jwt.encode(payload, PRIVATE_KEY, algorithm=ALGORITHM)

def verify_token(token: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(token, PUBLIC_KEY, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER)
        if not REQUIRED_CLAIMS.issubset(payload.keys()):
            raise HTTPException(status_code=401, detail="Token missing required claims")
        return payload
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_current_user(token: str = Depends(oauth2_scheme)) -> Dict[str, Any]:
    return verify_token(token)

# -------------------- Models --------------------
class LoginRequest(BaseModel):
    username: str
    role: str

class OnboardingRequest(BaseModel):
    device_id: str
    voucher: str

class ConsentRequest(BaseModel):
    policy_version: str
    consent_given: bool

class ComplianceRequest(BaseModel):
    workflow: Dict[str, Any]

# -------------------- Demo User Store --------------------
USERS = {
    "admin@example.com":   {"role": "admin"},
    "provider@example.com":{"role": "provider"},
    "patient@example.com": {"role": "patient"},
}

# -------------------- IoT Provisioning --------------------
class IoTProvisioning:
    def onboard_device(self, device_id: str, voucher: str) -> Dict[str, Any]:
        if not voucher.startswith("VOUCHER-"):
            raise ValueError("Invalid voucher format.")
        try:
            cur.execute(
                "INSERT INTO devices (device_id, status, trust_chain) VALUES (?, ?, ?)",
                (device_id, "Provisioned", True),
            )
            conn.commit()
            return {"device_id": device_id, "status": "Provisioned", "trust_chain": True}
        except sqlite3.IntegrityError:
            raise ValueError("Device already exists.")

# -------------------- Monitoring Generator --------------------
async def generate_sensor_data():
    while True:
        yield {
            "heart_rate": random.randint(60, 130),
            "temperature": round(random.uniform(36.0, 39.0), 1),
            "spo2": random.randint(90, 100),
        }
        await asyncio.sleep(3)

# -------------------- Endpoints --------------------
@app.get("/", tags=["System"], summary="Welcome")
async def root():
    return {"message": "IoT Healthcare Platform API", "docs": "/docs", "health": "/health"}

@app.get("/health", tags=["System"], summary="Health check")
async def health_check():
    return {"status": "API is running", "version": "2.0"}

@app.post("/login", tags=["Auth"], summary="Issue JWT (role-bound)")
async def login(request: LoginRequest):
    user = USERS.get(request.username)
    if not user or user["role"] != request.role:
        audit("login", request.username, "token", "fail", reason="user_role_mismatch")
        raise HTTPException(status_code=400, detail="Invalid credentials or role")
    token = create_token(request.username, request.role)
    audit("login", request.username, "token", "success")
    return {"access_token": token, "token_type": "bearer"}

@app.post("/consent", tags=["Compliance"], summary="Capture consent")
async def capture_consent(request: ConsentRequest, user: dict = Depends(get_current_user)):
    cur.execute(
        "INSERT INTO consents (username, policy_version, consent_given, timestamp) VALUES (?, ?, ?, ?)",
        (user.get("sub"), request.policy_version, request.consent_given, datetime.utcnow().isoformat()),
    )
    conn.commit()
    audit("consent.accept" if request.consent_given else "consent.revoke", user.get("sub"), request.policy_version, "success")
    return {"status": "ok"}

@app.post("/onboard", tags=["Devices"], summary="Onboard a device (RBAC: devices:write)")
async def onboard_device(request: OnboardingRequest, user: dict = Depends(get_current_user)):
    if not has_permission(user.get("role"), "devices:write"):
        raise HTTPException(status_code=403, detail="Not authorized by policy")
    provisioning = IoTProvisioning()
    try:
        device = provisioning.onboard_device(request.device_id, request.voucher)
        audit("device.onboard", user.get("sub"), request.device_id, "success")
        return {"message": "Device onboarded successfully", "device": device}
    except ValueError as e:
        audit("device.onboard", user.get("sub"), request.device_id, "fail", reason=str(e))
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/compliance", tags=["Compliance"], summary="Validate workflow compliance")
async def validate_compliance(request: ComplianceRequest, user: dict = Depends(get_current_user)):
    if user.get("role") not in ["admin", "provider"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    wf = request.workflow or {}
    rules: List[Dict[str, Any]] = security.get("rules", [])

    def has_requirements(rule_name: str, reqs: List[str]) -> bool:
        rule = next((r for r in rules if r.get("name", "").startswith(rule_name)), {})
        active = rule.get("status") == "enabled"
        listed = rule.get("requirements", [])
        return active and all(r in listed for r in reqs)

    details = []
    hipaa_ok = True
    gdpr_ok = True
    if wf.get("HIPAA"):
        hipaa_ok = has_requirements("HIPAA", ["encryption", "audit_logging", "role_based_access"])
        details.append({"HIPAA": hipaa_ok})
    if wf.get("GDPR"):
        gdpr_ok = has_requirements("GDPR", ["data_minimization", "consent_tracking"])
        details.append({"GDPR": gdpr_ok})

    passed = hipaa_ok and gdpr_ok
    audit("policy.check", user.get("sub"), "workflow", "pass" if passed else "fail")
    return {"compliance_passed": passed, "details": details}

@app.get("/architecture", tags=["Governance"], summary="Active modules")
async def get_architecture():
    return architecture

@app.get("/security", tags=["Governance"], summary="Current security posture")
async def get_security():
    sec = dict(security)
    sec.setdefault("jwt", {})
    sec["jwt"]["algorithm"] = ALGORITHM  # Force RS256 in runtime view
    return sec

@app.get("/workflows", tags=["Governance"], summary="Workflow steps")
async def get_workflows():
    return workflows

@app.websocket("/monitor")
async def monitor_data(websocket: WebSocket):
    await websocket.accept()
    async for data in generate_sensor_data():
        await websocket.send_json(data)

# -------------------- Main --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=True)