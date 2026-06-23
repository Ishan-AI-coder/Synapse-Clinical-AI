import streamlit as st
import requests
import uuid
import os
import time
import re
from datetime import datetime
from tavily import TavilyClient
from dotenv import load_dotenv
import streamlit.components.v1 as components

load_dotenv(override=True)
API_URL = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Clinical Support Center", page_icon="🩺", layout="wide")

# --- UI PROFESSIONAL POLISH (CUSTOM CSS) ---
st.markdown("""
    <style>
    .main {background-color: #fcfcfc;}
    div[data-testid="stExpander"] {
        border: 1px solid #eef2f6;
        border-radius: 8px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.02);
        margin-bottom: 10px;
    }
    .metric-card {
        background-color: #ffffff;
        border: 1px solid #e6ebf1;
        padding: 20px;
        border-radius: 10px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.01);
        text-align: center;
    }
    .metric-value {
        font-size: 28px;
        font-weight: 700;
        color: #1e293b;
    }
    .metric-label {
        font-size: 13px;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    </style>
""", unsafe_allow_html=True)

# --- STATE INITIALIZATION ---
if "token" not in st.session_state: st.session_state.token = None
if "chat_history" not in st.session_state: st.session_state.chat_history = []
if "thread_id" not in st.session_state: st.session_state.thread_id = str(uuid.uuid4())
if "triggered_query" not in st.session_state: st.session_state.triggered_query = None
if "pending_hitl" not in st.session_state: st.session_state.pending_hitl = False
if "agent_traces" not in st.session_state: st.session_state.agent_traces = []

def get_auth_headers(): return {"Authorization": f"Bearer {st.session_state.token}"}

# --- LIVE TAVILY NEWS FEED CONFIG ---
@st.cache_data(ttl=3600)
def fetch_live_medical_briefing():
    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        return [{"title": "API Key Missing", "content": "Set TAVILY_API_KEY inside your local environmental variables to populate.", "url": "#"}]
    try:
        client = TavilyClient(api_key=tavily_key)
        trusted_medical_sources = ["nejm.org", "thelancet.com", "nature.com", "nih.gov", "fda.gov", "bmj.com"]
        res = client.search(
            query="new clinical trials, FDA drug approvals, and medical research breakthroughs", 
            search_depth="advanced",
            topic="news",
            time_range="w",
            include_domains=trusted_medical_sources,
            max_results=4
        )
        return res.get("results", [])
    except Exception as e:
        return [{"title": "Network Offline", "content": f"Unable to fetch clinical items right now. Exception: {str(e)}", "url": "#"}]

# --- AUTHENTICATION GATE ---
if not st.session_state.token:
    st.subheader("🩺 CareScribe Authentication")
    with st.form("portal_login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Secure Login"):
            res = requests.post(f"{API_URL}/api/token", data={"username": username, "password": password})
            if res.status_code == 200:
                st.session_state.token = res.json()["access_token"]
                st.rerun()
            else: st.error("Access Denied. Invalid credentials.")
    st.stop()

# --- FETCH DATABASE DATA VIA API ---
try: 
    patients_res = requests.get(f"{API_URL}/api/patients", headers=get_auth_headers())
    patients = patients_res.json() if patients_res.status_code == 200 else []
except: 
    patients = []
p_map = {p["name"]: p for p in patients}

# --- INTAKE PANEL ---
with st.sidebar:
    st.title("🗂️ Intake Desk")
    with st.expander("Register New Case File", expanded=True):
        with st.form("intake_form"):
            name = st.text_input("Patient Legal Name")
            age = st.number_input("Age", min_value=0, max_value=125, step=1, value=30)
            bg = st.selectbox("Blood Group", ["Unassigned", "A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-"])
            contact = st.text_input("Primary Contact")
            email = st.text_input("Email Address")
            
            if st.form_submit_button("Commit to Registry") and name:
                payload = {"name": name, "age": age, "blood_group": bg, "contact": contact, "email": email}
                res = requests.post(f"{API_URL}/api/patients/register", json=payload, headers=get_auth_headers())
                if res.status_code == 200:
                    st.success(f"File created for {name}.")
                    time.sleep(1)
                    st.rerun()
                else: st.error("Failed to register patient.")
                
    st.markdown("---")
    if st.button("Logout Secure Session"):
        st.session_state.token = None
        st.rerun()

# --- 5 NAVIGATION TABS ---
t1, t2, t3, t4, t5 = st.tabs(["📅 Daily Guide", "👤 Patient 360 Workspace", "🎙️ Audio Scribe Node", "🧠 Diagnostic Copilot", "📑 Smart Lab Analyzer"])

# --- TAB 1: DAILY GUIDE ---
with t1:
    st.header("Daily dashboard")
    try: dash_data = requests.get(f"{API_URL}/api/dashboard", headers=get_auth_headers()).json()
    except: dash_data = {"appointments": []}
    
    m1, m2, m3 = st.columns(3)
    with m1: st.markdown(f"<div class='metric-card'><div class='metric-value'>{len(dash_data.get('appointments', []))}</div><div class='metric-label'>Scheduled Appointments Today</div></div>", unsafe_allow_html=True)
    with m2: st.markdown(f"<div class='metric-card'><div class='metric-value'>{len(p_map)}</div><div class='metric-label'>Total Registered Patients</div></div>", unsafe_allow_html=True)
    with m3: st.markdown(f"<div class='metric-card'><div class='metric-value'>Active</div><div class='metric-label'>CDSS Model Pipeline</div></div>", unsafe_allow_html=True)
    
    st.markdown("<br>", unsafe_allow_html=True)
    c_left, c_right = st.columns([1.8, 1.5])
    
    with c_left:
        st.subheader("Scheduled Appointments")
        if not dash_data.get("appointments"): 
            st.info("No active appointments found in local matrix for today.")
        for a in dash_data.get("appointments", []):
            st.markdown(f"🔹 **{a['time']}** — {a['patient_name']}")
            
    with c_right:
        st.subheader("Clinical Digest")
        news_items = fetch_live_medical_briefing()
        for item in news_items:
            with st.expander(f"📰 {item['title'][:65]}..."):
                st.markdown(item['content'])
                st.caption(f"[Source Dispatch]({item['url']})")

# --- TAB 2: PATIENT 360 WORKSPACE ---
with t2:
    st.header("Clinical records")
    if not p_map: 
        st.warning("Database registry empty. Register cases via intake panel.")
    else:
        selection = st.selectbox("Select patient", options=list(p_map.keys()))
        tgt = p_map[selection]
        
        st.markdown(f"## {tgt['name']} (Age: {tgt.get('age', 'N/A')} | Blood Group: {tgt.get('blood_group', 'Unknown')})")
        
        cx1, cx2, cx3 = st.columns(3)
        with cx1:
            st.markdown("**Chronic Conditions (ICD-10)**")
            if not tgt.get('chronic_conditions'): st.caption("No issues logged.")
            for c in tgt.get('chronic_conditions', []): st.markdown(f"- {c}")
        with cx2:
            st.markdown("**Active Pharmacology List**")
            if not tgt.get('current_medications'): st.caption("No entries logged.")
            for m in tgt.get('current_medications', []): st.markdown(f"- {m}")
        with cx3:
            st.markdown("**Allergies & Demographics**")
            st.caption(f"Contact: {tgt.get('contact', 'None')}")
            st.caption(f"Email: {tgt.get('email', 'None')}")
            st.caption(f"Allergic Matrix: {', '.join(tgt.get('allergies', [])) if tgt.get('allergies') else 'None Tracked'}")
            
        st.markdown("---")
        st.markdown("### Patient history")
        st.text_area("Rolling Memory Trace Output", value=tgt.get('summary', ''), height=220, disabled=True)
        
        if tgt.get('imaging_records'):
            st.markdown("### Imaging Records & Diagnostics")
            img_cols = st.columns(3)
            for idx, record in enumerate(tgt['imaging_records']):
                with img_cols[idx % 3]:
                    with st.expander(f"Scan Log: {record.get('timestamp', '')[:10]}"):
                        if 'file_path' in record and os.path.exists(record['file_path']):
                            st.image(record['file_path'], use_container_width=True)
                        st.caption(f"**Query:** {record.get('query', 'N/A')}")
                        st.markdown(f"**Analysis:** {record.get('analysis', 'No data')}")

        st.markdown("---")
        ehr_payload = (
            f"--- ELECTRONIC HEALTH RECORD (EHR) EXPORT ---\n"
            f"Patient Name: {tgt['name']}\nAge: {tgt.get('age')}\nEmail: {tgt.get('email')}\nContact: {tgt.get('contact')}\n\n"
            f"[ACTIVE CONDITIONS]: {', '.join(tgt.get('chronic_conditions', [])) if tgt.get('chronic_conditions') else 'None Recorded'}\n"
            f"[CURRENT MEDICATIONS]: {', '.join(tgt.get('current_medications', [])) if tgt.get('current_medications') else 'None Recorded'}\n\n"
            f"[MASTER CLINICAL SUMMARY]:\n{tgt.get('summary', '')}\n"
            f"\nExport Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        st.download_button(
            label="📥 Export to EHR (Download Master File)", data=ehr_payload,
            file_name=f"{tgt['name'].replace(' ', '_')}_EHR_Export.txt", mime="text/plain", type="primary"
        )

# --- TAB 3: AUDIO SCRIBE NODE ---
with t3:
    st.header("🎙️ Acoustic Capture Node")
    if p_map:
        active_name = st.selectbox("Assign Audio Session Capture Target", options=list(p_map.keys()), key="sc_box")
        
        input_method = st.radio("Select Audio Source:", ["Record Live", "Upload Audio File"], horizontal=True)
        audio_bytes = None
        content_type = "audio/wav"
        
        if input_method == "Record Live":
            audio_record = st.audio_input(f"Capture consultation link for {active_name}")
            if audio_record:
                audio_bytes = audio_record.getvalue()
                content_type = audio_record.type
        else:
            audio_upload = st.file_uploader("Upload consultation audio file", type=["wav", "mp3", "m4a", "ogg"])
            if audio_upload:
                audio_bytes = audio_upload.getvalue()
                content_type = audio_upload.type

        if audio_bytes and st.button("Execute Pipeline & Ingest to Schema", type="primary"):
            with st.status("Initializing AI Audio Scribe Pipeline...", expanded=True) as status:
                st.write("📡 Uploading secure byte stream to API...")
                
                files = {"audio": ("audio.wav", audio_bytes, content_type)}
                data = {"patient_id": p_map[active_name]["id"]}
                res = requests.post(f"{API_URL}/api/scribe/process", files=files, data=data, headers=get_auth_headers())
                
                if res.status_code == 200:
                    status.update(label="Ingestion successful. Safety protocols executed.", state="complete", expanded=False)
                    with st.expander("Review Acoustic Transcript Matrix"):
                        st.text(res.json().get("processed_transcript"))
                else:
                    status.update(label="Pipeline Failure", state="error", expanded=True)
                    st.error(f"API Error: {res.text}")

# --- TAB 4: DIAGNOSTIC COPILOT ---
with t4:
    st.header("🧠 Clinical Co-Pilot Core")
    context_name = st.selectbox("Inject Target Identity Context Buffer:", options=["None"] + list(p_map.keys()))
    
    with st.expander("Upload Medical Scans (CT, MRI, X-Ray) & Prescriptions"):
        scans = st.file_uploader("Attach Image Assets", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
        
    box = st.container(height=380)
    with box:
        for h in st.session_state.chat_history:
            with st.chat_message(h["role"]):
                st.markdown(h["content"])

    if st.button("Reset Session Memory Topology"):
        st.session_state.chat_history = []
        st.session_state.thread_id = str(uuid.uuid4())
        st.rerun()

    if st.session_state.pending_hitl:
        st.warning("⚠️ Graph Interrupted: Clinical action requires review.")
        if st.button("Approve & Resume"):
            payload = {"query": "", "thread_id": st.session_state.thread_id, "resume_hitl": True}
            res = requests.post(f"{API_URL}/api/copilot/chat", json=payload, headers=get_auth_headers()).json()
            st.session_state.pending_hitl = False
            
            clean_text = res.get("response", "Approval registered.")
            followups = res.get("followups", [])
            
            st.session_state.chat_history.append({"role": "assistant", "content": clean_text})
            if res.get("trace"): st.session_state.agent_traces = res.get("trace")
            st.rerun()

    user_input = st.chat_input("Enter clinical diagnostic directives...")
    query = user_input or st.session_state.triggered_query

    if query:
        st.session_state.triggered_query = None 
        injection = ""
        
        if scans and context_name != "None":
            os.makedirs("patient_uploads", exist_ok=True)
            for i, scan in enumerate(scans):
                safe_name = context_name.replace(" ", "_")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                permanent_path = f"patient_uploads/{safe_name}_{timestamp}_{i}.jpg"
                
                with open(permanent_path, "wb") as f: 
                    f.write(scan.getbuffer())
                
                injection += f"\n\n[SYSTEM NOTE: Asset permanently uploaded at '{permanent_path}'. Trigger analyze_medical_imaging tool for {context_name}.]"
        elif scans and context_name == "None":
            st.error("Please select a Target Identity Context above to attach images to a specific patient file.")
            st.stop()
            
        if context_name != "None":
            injection += f"\n\n[SYSTEM NOTE: Core subject contextual bounds constrained to '{context_name}']."

        st.session_state.chat_history.append({"role": "user", "content": query})
        with box:
            with st.chat_message("user"): 
                st.markdown(query)
            with st.chat_message("assistant"):
                with st.status("Agent analyzing query...", expanded=True) as status:
                    
                    chosen_id = p_map[context_name]["id"] if context_name != "None" else None
                    payload = {"query": query + injection, "patient_id": chosen_id, "thread_id": st.session_state.thread_id}
                    res = requests.post(f"{API_URL}/api/copilot/chat", json=payload, headers=get_auth_headers())
                    
                    if res.status_code == 200:
                        data = res.json()
                        if data.get("status") == "requires_approval":
                            st.session_state.pending_hitl = True
                            st.rerun()
                        else:
                            clean_text = data.get("response", "⚠️ [System Notice]: Empty response from the backend.")
                            followups = data.get("followups", [])
                            st.session_state.agent_traces = data.get("trace", [])
                            status.update(label="Analysis complete", state="complete", expanded=False)
                            
                            st.markdown(clean_text)
                            st.session_state.chat_history.append({"role": "assistant", "content": clean_text})
                            
                            if followups:
                                st.markdown("<br>**Suggested Actions:**", unsafe_allow_html=True)
                                cols = st.columns(len(followups))
                                for idx, suggestion in enumerate(followups):
                                    with cols[idx]:
                                        if st.button(suggestion, key=f"btn_{len(st.session_state.chat_history)}_{idx}"):
                                            st.session_state.triggered_query = suggestion
                                            st.rerun()
                    else:
                        status.update(label="Pipeline Failure", state="error", expanded=True)
                        st.error(f"Backend HTTP Error: {res.status_code}")

# --- TAB 5: SMART LAB ANALYZER ---
with t5:
    st.header("📑 OCR Lab Report Analysis")
    st.caption("Upload physical PDF/Image reports for auto-extraction and visual trend analysis.")
    
    try:
        with open("report_ui.html", "r", encoding="utf-8") as f:
            html_content = f.read()
        components.html(html_content, height=1200, scrolling=True)
    except FileNotFoundError:
        st.error("UI Asset 'report_ui.html' not found. Please create this file in the root directory and paste your HTML/JS code inside.")