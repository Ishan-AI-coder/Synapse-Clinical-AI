# test_db.py
from database import init_db, SessionLocal, Patient, Consultation, Appointment
from datetime import datetime

print("🔄 Initializing local PostgreSQL schema...")
init_db()
print("✅ Local tables created successfully!")

# Let's simulate a patient check-in and how the rolling feature updates
db = SessionLocal()

try:
    # 1. Register a new patient with extensive medical JSONB columns
    test_patient = Patient(
        name="John Doe",
        contact="+91 9876543210",
        blood_group="O+",
        allergies=["Penicillin"],
        chronic_conditions=["Hypertension"],
        current_medications=["Lisinopril 10mg daily"],
        past_surgeries=["Appendectomy (2018)"],
        clinical_summary="Initial intake complete."
    )
    db.add(test_patient)
    db.commit()
    db.refresh(test_patient)
    print(f"\n👤 Patient registered: {test_patient.name} (ID: {test_patient.id})")

    # 2. Simulate an audio session transcription being recorded
    transcript_text = "Doctor: Your blood pressure is looking better today, John. Let's keep you on Lisinopril but add Metformin 500mg for your newly presenting elevated fasting blood sugar."
    
    # Save the raw consultation chronologically to drive the rolling memory engine
    new_consult = Consultation(
        patient_id=test_patient.id,
        transcript=transcript_text,
        extracted_data={
            "new_prescriptions": ["Metformin 500mg"],
            "vitals": {"blood_pressure": "120/80"}
        }
    )
    db.add(new_consult)
    
    # 3. Apply the ROLLING FEATURE logic: Append and reconstruct the master summary
    # In production, Gemini will do this, but this is the database-level mechanics:
    updated_summary = (
        f"{test_patient.clinical_summary}\n\n"
        f"--- Update ({datetime.now().strftime('%Y-%m-%d')}) ---\n"
        f"Patient presented with elevated blood sugar. Prescribed Metformin. Vitals normal."
    )
    
    test_patient.clinical_summary = updated_summary
    
    # Append the newly discovered medication dynamically into the JSONB array
    updated_meds = list(test_patient.current_medications)
    updated_meds.append("Metformin 500mg")
    test_patient.current_medications = updated_meds
    
    db.commit()
    print("📈 Dynamic rolling memory summary updated successfully!")

finally:
    db.close()