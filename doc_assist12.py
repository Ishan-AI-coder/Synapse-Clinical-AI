import os
import base64
import requests
import warnings
import json
from datetime import datetime
from typing import TypedDict, Annotated, Sequence
from operator import add as add_messages
from dotenv import load_dotenv
from contextlib import contextmanager

warnings.filterwarnings("ignore", category=DeprecationWarning)

from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END, START
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from langchain_google_genai import HarmCategory, HarmBlockThreshold

from qdrant_client.http import models
from langchain_qdrant import QdrantVectorStore, RetrievalMode, FastEmbedSparse
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings

from database import SessionLocal, Patient, Appointment, Consultation

load_dotenv(override=True)
os.environ["FASTEMBED_CACHE_PATH"] = "./model_cache"

@contextmanager
def get_db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- AUDIO PIPELINE EXTRACTIONS ---
class SessionExtraction(BaseModel):
    has_appointment: bool = Field(description="True if a future appointment was scheduled.")
    appointment_date: str = Field(description="The scheduled date/time in YYYY-MM-DD HH:MM format.", default="")
    doctor_name: str = Field(description="Name of the doctor.", default="Unknown Doctor")
    new_diagnoses: list[str] = Field(description="Conditions discussed today.", default_factory=list)
    prescribed_medications: list[str] = Field(description="Medicines discussed today.", default_factory=list)

def ensure_list(item):
    if not item: return []
    if isinstance(item, str): return [item]
    if isinstance(item, list): return item
    try: return list(item)
    except Exception: return []

def extract_text(content):
    if isinstance(content, str): return content
    if isinstance(content, list): return " ".join([block.get("text", "") if isinstance(block, dict) else str(block) for block in content])
    return str(content)

def init_vector_store():
    dense_embeddings = FastEmbedEmbeddings(model_name="BAAI/bge-base-en-v1.5")
    sparse_embeddings = FastEmbedSparse(model_name="prithivida/Splade_PP_en_v1")
    dummy_doc = Document(page_content="System initialized.", metadata={"patient_id": "system", "type": "init"})
    return QdrantVectorStore.from_documents(
        documents=[dummy_doc], embedding=dense_embeddings, sparse_embedding=sparse_embeddings,
        path="./clinical_vector_storage", collection_name="patient_records", retrieval_mode=RetrievalMode.HYBRID
    )

def process_audio_session(audio_bytes: bytes, qdrant_store, patient_id: str, mime_type: str = "audio/wav", progress_callback=None):
    llm = ChatGoogleGenerativeAI(model="gemini-3.5-flash", temperature=0)
    
    if progress_callback: progress_callback(0.2, "Transcribing consultation audio...")
    b64_encoded = base64.b64encode(audio_bytes).decode("utf-8")
    transcription_prompt = "Listen to this doctor-patient conversation and provide a verbatim transcript. Prefix speakers as 'Doctor:' and 'Patient:'."
    msg = HumanMessage(content=[{"type": "text", "text": transcription_prompt}, {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_encoded}"}}])
    
    ai_msg = llm.invoke([msg])
    transcript = extract_text(ai_msg.content)
    
    if progress_callback: progress_callback(0.4, "Running structural clinical extraction...")
    extraction_prompt = f"Analyze this transcript. Today is {datetime.now().strftime('%A, %Y-%m-%d %H:%M')}. Extract details.\nTranscript:\n{transcript}"
    extracted_data = llm.with_structured_output(SessionExtraction).invoke(extraction_prompt)
    
    with get_db_session() as db:
        patient = db.query(Patient).filter(Patient.id == patient_id).first()
        if not patient: raise ValueError("Patient ID not found in database.")
        
        if extracted_data.has_appointment and extracted_data.appointment_date:
            try:
                appt_date = datetime.strptime(extracted_data.appointment_date, "%Y-%m-%d %H:%M")
                db.add(Appointment(date=appt_date, doctor_name=extracted_data.doctor_name, patient_id=patient_id, doctor_id=patient.doctor_id))
            except ValueError: pass 

        safety_alert = ""
        if extracted_data.prescribed_medications:
            if progress_callback: progress_callback(0.5, "Running DDI safety cross-checks...")
            safety_prompt = f"""
            You are a clinical safety module. Patient Allergies: {patient.allergies}
            Current Active Medications: {patient.current_medications}
            Newly Prescribed Medications: {extracted_data.prescribed_medications}
            Check for severe drug-drug interactions (DDI) or allergy conflicts. If NONE, output: SAFE. Otherwise start with '🚨 DDI/ALLERGY ALERT:'
            """
            safety_res = extract_text(llm.invoke(safety_prompt).content).strip()
            if "SAFE" not in safety_res: safety_alert = f"\n\n{safety_res}"
                
        db.add(Consultation(patient_id=patient_id, transcript=transcript + safety_alert, extracted_data={"new_diagnoses": extracted_data.new_diagnoses, "new_medications": extracted_data.prescribed_medications}))
                
        if progress_callback: progress_callback(0.7, "Compounding rolling clinical memory summary...")
        memory_prompt = f"Update this summary.\nOld: {patient.clinical_summary}\nNew context: {transcript}\n{safety_alert}\nKeep it professional."
        patient.clinical_summary = extract_text(llm.invoke(memory_prompt).content)
        
        patient.chronic_conditions = list(set(ensure_list(patient.chronic_conditions)).union(set(ensure_list(getattr(extracted_data, 'new_diagnoses', [])))))
        patient.current_medications = list(set(ensure_list(patient.current_medications)).union(set(ensure_list(getattr(extracted_data, 'prescribed_medications', [])))))
            
        db.commit()
                
    if progress_callback: progress_callback(0.9, "Indexing updates to persistent vector space...")
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=150)
    qdrant_store.add_documents(text_splitter.split_documents([Document(page_content=transcript + safety_alert, metadata={"patient_id": patient_id, "type": "consultation", "timestamp": datetime.now().timestamp()})]))
    
    if progress_callback: progress_callback(1.0, "Session complete.")
    return transcript + safety_alert

# --- LANGGRAPH AGENT & BULLETPROOF TOOLS ---
class AgentState(TypedDict): 
    messages: Annotated[Sequence[BaseMessage], add_messages]
    doctor_id: str

def create_doctor_copilot_agent(qdrant_store: QdrantVectorStore):
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash", 
        temperature=0, 
        max_output_tokens=4096,
        max_retries=3,
        timeout=120.0,
        safety_settings={
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        }
    )
    
    # --- 1. EXPLICIT RIGID SCHEMAS (Fixes Gemini JSON Crashes) ---
    class SearchPatientSchema(BaseModel):
        patient_name: str = Field(description="Name of the patient.")
        medical_query: str = Field(description="The specific medical question to look up.")

    class ScheduleSchema(BaseModel):
        date_str: str = Field(description="Date in YYYY-MM-DD format. Pass 'today' if the user doesn't specify.")

    class AnalyzeImagingSchema(BaseModel):
        patient_name: str = Field(description="Name of the patient.")
        image_path: str = Field(description="The file path of the image asset provided in the system note.")
        query: str = Field(description="Specific instructions on what to analyze in the scan.")

    class ClinicalReasoningSchema(BaseModel):
        clinical_query: str = Field(description="The medical scenario to analyze.")

    class SearchWebSchema(BaseModel):
        query: str = Field(description="The medical search query.")

    # --- 2. BULLETPROOF FUNCTIONS (No default arguments allowed) ---
    def search_patient_records(patient_name: str, medical_query: str) -> str:
        with get_db_session() as db:
            patient = db.query(Patient).filter(Patient.name.ilike(f"%{patient_name}%")).first()
            if not patient: return f"No patient records found under '{patient_name}'."
            qdrant_filter = models.Filter(must=[models.FieldCondition(key="metadata.patient_id", match=models.MatchValue(value=patient.id))])
            raw_docs = qdrant_store.as_retriever(search_kwargs={"k": 5, "filter": qdrant_filter}).invoke(medical_query)
            return f"--- MASTER RECORD ---\nConditions: {patient.chronic_conditions}\nMedications: {patient.current_medications}\nSummary: {patient.clinical_summary}\n\n" + "\n\n".join([f"--- Context Segment ---\n{d.page_content}" for d in raw_docs])

    def check_doctor_schedule(date_str: str) -> str:
        with get_db_session() as db:
            try: t_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str != 'today' else datetime.now().date()
            except ValueError: t_date = datetime.now().date()
            s_day, e_day = datetime.combine(t_date, datetime.min.time()), datetime.combine(t_date, datetime.max.time())
            appts = db.query(Appointment).join(Patient).filter(Appointment.date >= s_day, Appointment.date <= e_day).all()
            if not appts: return f"Schedule clear for {t_date}."
            return "\n".join([f"- {a.date.strftime('%I:%M %p')} : {a.patient.name}" for a in appts])

    def analyze_medical_imaging(patient_name: str, image_path: str, query: str) -> str:
        with get_db_session() as db:
            patient = db.query(Patient).filter(Patient.name.ilike(f"%{patient_name}%")).first()
            if not patient or not os.path.exists(image_path): 
                return "Execution aborted: Patient profile or image file missing/unauthorized."
            
            with open(image_path, "rb") as f: b64 = base64.b64encode(f.read()).decode("utf-8")
                
            api_url = "http://localhost:11434/api/chat"
            payload = {
                "model": "medgemma",
                "messages": [{"role": "user", "content": query, "images": [b64]}],
                "stream": False,
                "options": {"num_predict": 2048}
            }
            try:
                res = requests.post(api_url, json=payload, timeout=180).json()
                if "error" in res: return f"Ollama Engine Error: {res['error']}"
                analysis = res["message"]["content"]
                
                current_records = ensure_list(patient.imaging_records)
                current_records.append({"timestamp": datetime.now().isoformat(), "query": query, "analysis": analysis, "file_path": image_path})
                patient.imaging_records = current_records
                patient.clinical_summary += f"\n\n[Local MedGemma Scan Analysis]: {analysis}"
                db.commit()
                return f"Scan successfully analyzed and attached to {patient_name}'s record. Findings:\n{analysis}"
            except Exception as e:
                return f"Local Ollama API Failure: {str(e)}"

    def medgemma_clinical_reasoning(clinical_query: str) -> str:
        api_url = "http://localhost:11434/api/chat"
        payload = {
            "model": "medgemma",
            "messages": [{"role": "user", "content": f"Provide deep clinical reasoning and differential diagnosis for: {clinical_query}"}],
            "stream": False
        }
        try:
            res = requests.post(api_url, json=payload).json()
            if "error" in res: return f"Ollama Engine Error: {res['error']}"
            return res["message"]["content"]
        except Exception as e:
            return f"Ollama Reasoning Failed: {str(e)}"

    def search_medical_web(query: str) -> str:
        tavily_key = os.getenv("TAVILY_API_KEY")
        if not tavily_key: return "Search engine offline: TAVILY_API_KEY missing."
        from tavily import TavilyClient
        try:
            results = TavilyClient(api_key=tavily_key).search(query=query, search_depth="basic", max_results=3).get("results", [])
            return "\n\n".join([f"Source: {r['url']}\nContent: {r['content']}" for r in results]) if results else "No live literature found."
        except Exception as e: return f"Search engine offline: {str(e)}"

    # --- 3. BINDING WITH EXPLICIT SCHEMAS ---
    tools = [
        StructuredTool.from_function(func=search_patient_records, name="search_patient_database", description="Fetch patient ledger metrics.", args_schema=SearchPatientSchema),
        StructuredTool.from_function(func=check_doctor_schedule, name="check_appointments", description="Fetch operational calendar listings.", args_schema=ScheduleSchema),
        StructuredTool.from_function(func=search_medical_web, name="search_medical_web", description="Browse live literature and standards.", args_schema=SearchWebSchema),
        StructuredTool.from_function(func=analyze_medical_imaging, name="analyze_medical_imaging", description="Analyze physical CT scans, MRIs, and X-Rays using MedGemma.", args_schema=AnalyzeImagingSchema),
        StructuredTool.from_function(func=medgemma_clinical_reasoning, name="medgemma_clinical_reasoning", description="Use exclusively for deep clinical reasoning.", args_schema=ClinicalReasoningSchema)
    ]
    llm_with_tools = llm.bind_tools(tools)
    
    def call_llm(state: AgentState):
        # --- BRAIN 1: THE ROUTER ---
        router_prompt = (
            f"You are a Clinical AI Copilot. Today is {datetime.now().strftime('%A, %Y-%m-%d')}. "
            "Your ONLY job is to use MedGemma tools to fetch data or analyze images. "
            "CRITICAL: Keep all tool arguments extremely short (under 10 words). Do NOT put complex reasoning into tool arguments. "
            "If you have gathered enough information, draft a complete text response to the user."
        )
        
        response = llm_with_tools.invoke([SystemMessage(content=router_prompt)] + list(state['messages']))
        
        if getattr(response, 'tool_calls', None):
            return {'messages': [response]}
            
        # --- BRAIN 2: THE FORMATTER (Strict Pydantic Schema) ---
        safe_text = extract_text(response.content).strip()
        
        if safe_text.lower() in ["", "[]", "null", "none"]:
            print("\n[AUTO-FIX] Router hallucinated an empty draft. Passing raw query to Formatter...")
            draft_content = "Please provide a clinical summary based on the history."
            for msg in reversed(state['messages']):
                if msg.type == 'human':
                    draft_content = extract_text(msg.content)
                    break
        else:
            draft_content = safe_text

        chat_llm = ChatGoogleGenerativeAI(
            model="gemini-3.5-flash", 
            temperature=0.3, 
            max_output_tokens=4096,
            safety_settings={
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            }
        )
        
        # Define strict output schema
        class FormattedOutput(BaseModel):
            clinical_response: str = Field(description="The clear, professional, conversational clinical text for the user.")
            suggested_actions: list[str] = Field(description="Exactly 2 to 3 short follow-up questions the doctor might ask next.")
            
        structured_llm = chat_llm.with_structured_output(FormattedOutput)

        formatter_prompt = SystemMessage(
            content="You are a Clinical AI Copilot. Read the provided clinical draft and present it to the user in a clear, professional, and conversational manner."
        )
        
        try:
            final_obj = structured_llm.invoke([formatter_prompt, HumanMessage(content=f"Clinical Draft to Format: {draft_content}")])
            final_response = AIMessage(content=final_obj.model_dump_json())
        except Exception as e:
            print(f"\n[CRITICAL] Formatting completely failed: {e}. Injecting hardcoded bypass...")
            fallback_data = {
                "clinical_response": "I have processed the clinical request, but the output formatter failed. Please check the raw diagnostic traces or rephrase your query.",
                "suggested_actions": ["Review patient master record.", "Check today's appointment schedule."]
            }
            final_response = AIMessage(content=json.dumps(fallback_data))
            
        return {'messages': [final_response]}

    def should_continue(state: AgentState): 
        return "retriever_agent" if hasattr(state['messages'][-1], 'tool_calls') and len(state['messages'][-1].tool_calls) > 0 else END
        
    graph = StateGraph(AgentState)
    graph.add_node("llm", call_llm)
    graph.add_node("retriever_agent", ToolNode(tools=tools))
    
    graph.add_edge(START, "llm")
    graph.add_conditional_edges("llm", should_continue)
    graph.add_edge("retriever_agent", "llm")
    
    return graph.compile(checkpointer=MemorySaver(), interrupt_before=["retriever_agent"])