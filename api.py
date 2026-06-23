import os
import bcrypt
import uuid
import asyncio
import json
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from jose import JWTError, jwt
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import Base, engine, Doctor, Patient, Appointment, SessionLocal
from doc_assist12 import create_doctor_copilot_agent, init_vector_store, process_audio_session, extract_text
from langchain_core.messages import HumanMessage

# --- 1. AUTHENTICATION LOGIC ---
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-production-key-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 600

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/token")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

def get_password_hash(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_doctor(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, 
        detail="Could not validate credentials", 
        headers={"WWW-Authenticate": "Bearer"}
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        doctor_id: str = payload.get("sub")
        if doctor_id is None: raise credentials_exception
    except JWTError:
        raise credentials_exception
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if doctor is None: raise credentials_exception
    return doctor


# --- 2. FASTAPI SYSTEM INITIALIZATION ---
Base.metadata.create_all(bind=engine)

VECTOR_STORE = None
GRAPH = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global VECTOR_STORE, GRAPH
    print("Initializing Qdrant Vector Store and LangGraph Agent...")
    VECTOR_STORE = init_vector_store()
    GRAPH = create_doctor_copilot_agent(VECTOR_STORE)
    print("AI Components Online.")
    yield

app = FastAPI(title="CareScribe Secure API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    query: str
    patient_id: Optional[str] = None
    thread_id: Optional[str] = None
    resume_hitl: bool = False


# --- 3. API ENDPOINTS ---
@app.post("/api/token")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    db = SessionLocal()
    doctor = db.query(Doctor).filter(Doctor.username == form_data.username).first()
    if not doctor or not verify_password(form_data.password, doctor.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    access_token = create_access_token(data={"sub": doctor.id})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/dashboard")
def get_dashboard_data(current_doctor: Doctor = Depends(get_current_doctor), db: Session = Depends(get_db)):
    s_day = datetime.combine(datetime.today(), datetime.min.time())
    e_day = datetime.combine(datetime.today(), datetime.max.time())
    appts = db.query(Appointment).filter(Appointment.doctor_id == current_doctor.id, Appointment.date >= s_day, Appointment.date <= e_day).all()
    return {"appointments": [{"date": a.date.isoformat(), "patient_name": a.patient.name, "time": a.date.strftime('%I:%M %p')} for a in appts]}

@app.get("/api/patients")
def list_patients(current_doctor: Doctor = Depends(get_current_doctor), db: Session = Depends(get_db)):
    patients = db.query(Patient).filter(Patient.doctor_id == current_doctor.id).all()
    return [{
        "id": p.id, "name": p.name, "summary": p.clinical_summary, "age": p.age,
        "blood_group": p.blood_group, "contact": p.contact, "email": p.email,
        "allergies": p.allergies, "chronic_conditions": p.chronic_conditions,
        "current_medications": p.current_medications, "imaging_records": p.imaging_records,
        "family_history": p.family_history, "past_surgeries": p.past_surgeries
    } for p in patients]

class PatientCreate(BaseModel):
    name: str
    age: Optional[int] = None
    blood_group: Optional[str] = None
    contact: Optional[str] = None
    email: Optional[str] = None
    allergies: Optional[List[str]] = []
    past_surgeries: Optional[List[str]] = []
    family_history: Optional[List[str]] = []

@app.post("/api/patients/register")
def register_patient(patient_data: PatientCreate, current_doctor: Doctor = Depends(get_current_doctor), db: Session = Depends(get_db)):
    try:
        new_patient = Patient(
            doctor_id=current_doctor.id, name=patient_data.name, age=patient_data.age,
            blood_group=patient_data.blood_group, contact=patient_data.contact, email=patient_data.email,
            allergies=patient_data.allergies, past_surgeries=patient_data.past_surgeries, family_history=patient_data.family_history
        )
        db.add(new_patient)
        db.commit()
        return {"status": "success", "patient_id": new_patient.id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/scribe/process")
async def process_scribe_audio(patient_id: str = Form(...), audio: UploadFile = File(...), current_doctor: Doctor = Depends(get_current_doctor)):
    try:
        audio_bytes = await audio.read()
        transcript = await asyncio.to_thread(process_audio_session, audio_bytes, VECTOR_STORE, patient_id, audio.content_type)
        return {"status": "success", "processed_transcript": transcript}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    


import base64
from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

# --- OCR AI EXTRACTION SCHEMA ---
class LabReportExtraction(BaseModel):
    name: str = Field(description="Patient name. Leave empty if none.", default="")
    age: str = Field(description="Patient age in years.", default="")
    sex: str = Field(description="Patient sex (male/female/unspecified).", default="unspecified")
    hb: float = Field(description="Hemoglobin (Hb) in g/dL", default=None)
    wbc: float = Field(description="Total WBC / Leucocyte Count in 10^9/L (If given in cumm like 9000, convert to 9.0)", default=None)
    plt: float = Field(description="Platelet Count in 10^9/L (If given in cumm like 150000, convert to 150)", default=None)
    glu: float = Field(description="Fasting Glucose / FBS in mg/dL", default=None)
    tsh: float = Field(description="TSH in mIU/L", default=None)
    ldl: float = Field(description="LDL Cholesterol in mg/dL", default=None)
    hdl: float = Field(description="HDL Cholesterol in mg/dL", default=None)
    tg: float = Field(description="Triglycerides in mg/dL", default=None)
    cr: float = Field(description="Serum Creatinine in mg/dL", default=None)
    urea: float = Field(description="Urea in mg/dL", default=None)
    bilirubin: float = Field(description="Bilirubin in mg/dL", default=None)
    potassium: float = Field(description="Potassium in mmol/L", default=None)
    calcium: float = Field(description="Calcium in mg/dL", default=None)
    vitamind: float = Field(description="Vitamin D in ng/mL", default=None)
    b12: float = Field(description="Vitamin B12 in pg/mL", default=None)
    ferritin: float = Field(description="Ferritin in ng/mL", default=None)
    folate: float = Field(description="Folate / Folic Acid in ng/mL", default=None)
    magnesium: float = Field(description="Magnesium in mg/dL", default=None)
    crp: float = Field(description="C-reactive protein (CRP) in mg/L", default=None)

@app.post("/api/ocr/extract")
async def extract_lab_report(file: UploadFile = File(...), current_doctor: Doctor = Depends(get_current_doctor)):
    try:
        bytes_data = await file.read()
        b64_data = base64.b64encode(bytes_data).decode("utf-8")
        
        # Gemini 3.5 Flash is perfect and fast for OCR tasks
        llm = ChatGoogleGenerativeAI(model="gemini-3.5-flash", temperature=0)
        structured_llm = llm.with_structured_output(LabReportExtraction)
        
        prompt = (
            "You are an expert medical data extractor. Read this pathology report. "
            "Extract the requested patient details and lab values exactly as they appear. "
            "CRITICAL: If WBC or Platelets are given in 'cumm' (e.g. 9000 or 150000), you MUST convert them to 10^9/L by dividing by 1000 (e.g. 9.0 or 150)."
        )
        
        msg = HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{file.content_type};base64,{b64_data}"}}
        ])
        
        result = structured_llm.invoke([msg])
        return {"status": "success", "data": result.dict()}
        
    except Exception as e:
        print(f"OCR Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/copilot/chat")
async def chat_copilot(request: ChatRequest, current_doctor: Doctor = Depends(get_current_doctor)):
    thread = request.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread}}
    
    try:
        if request.resume_hitl:
            GRAPH.invoke({"doctor_id": current_doctor.id}, config)
        else:
            state_input = {"messages": [HumanMessage(content=request.query)], "doctor_id": current_doctor.id}
            GRAPH.invoke(state_input, config)
        
        state = GRAPH.get_state(config)
        if state.next: 
            return {"status": "requires_approval", "pending_node": state.next[0], "thread_id": thread}

        final_msg = state.values['messages'][-1]
        
        agent_trace = []
        for msg in state.values['messages']:
            if hasattr(msg, 'name') and msg.name:
                agent_trace.append(f"Executed Tool: {msg.name}")
                
        raw_content = getattr(final_msg, "content", "")
        
        print("\n--- DEBUG RAW OUTPUT ---")
        print(f"Content: '{raw_content}'")
        print("------------------------\n")

        try:
            parsed_data = json.loads(raw_content)
            safe_response = parsed_data.get("clinical_response", "Error reading response.")
            followups = parsed_data.get("suggested_actions", [])
        except json.JSONDecodeError:
            safe_response = raw_content if raw_content else "⚠️ **[System Notice]**: Gemini intercepted and scrubbed this response."
            followups = []
            
        return {"status": "success", "response": safe_response, "followups": followups, "trace": agent_trace, "thread_id": thread}
        
    except Exception as e:
        print(f"\n--- API CRASH --- \n{e}\n")
        return {"status": "error", "response": f"⚠️ Backend Error: {str(e)}", "trace": [], "thread_id": thread}