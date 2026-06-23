import uuid
import asyncio
import json
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import Base, engine, Doctor, Patient, Appointment, SessionLocal
from app12 import create_access_token, get_current_doctor, verify_password, get_password_hash, get_db
from doc_assist12 import create_doctor_copilot_agent, init_vector_store, process_audio_session, extract_text
from langchain_core.messages import HumanMessage

Base.metadata.create_all(bind=engine)

# 1. Define global variables for your AI components
VECTOR_STORE = None
GRAPH = None

# 2. Use a lifespan manager to initialize AI components ONLY in the worker process
@asynccontextmanager
async def lifespan(app: FastAPI):
    global VECTOR_STORE, GRAPH
    print("Initializing Qdrant Vector Store and LangGraph Agent...")
    VECTOR_STORE = init_vector_store()
    GRAPH = create_doctor_copilot_agent(VECTOR_STORE)
    print("AI Components Online.")
    yield

# 3. Pass the lifespan to the FastAPI app
app = FastAPI(title="CareScribe Secure API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    query: str
    patient_id: Optional[str] = None
    thread_id: Optional[str] = None
    resume_hitl: bool = False

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