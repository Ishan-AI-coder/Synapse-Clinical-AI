import os
import uuid
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

load_dotenv(override=True)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:Kanaklata@2005@localhost:5432/hospital_db")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Doctor(Base):
    __tablename__ = 'doctors'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    
    patients = relationship("Patient", back_populates="doctor")
    appointments = relationship("Appointment", back_populates="doctor")

class Patient(Base):
    __tablename__ = 'patients'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    doctor_id = Column(String, ForeignKey('doctors.id', ondelete='CASCADE'), nullable=False)
    name = Column(String, nullable=False, index=True)
    age = Column(Integer, nullable=True)
    contact = Column(String, nullable=True)
    email = Column(String, nullable=True)
    blood_group = Column(String, nullable=True)
    
    allergies = Column(JSONB, default=list, server_default='[]')
    chronic_conditions = Column(JSONB, default=list, server_default='[]')
    current_medications = Column(JSONB, default=list, server_default='[]')
    
    # RESTORED: Deep medical history columns
    family_history = Column(JSONB, default=list, server_default='[]')
    past_surgeries = Column(JSONB, default=list, server_default='[]')
    
    imaging_records = Column(JSONB, default=list, server_default='[]') 
    clinical_summary = Column(Text, default="New patient file created.")
    created_at = Column(DateTime, default=datetime.utcnow)
    
    doctor = relationship("Doctor", back_populates="patients")
    appointments = relationship("Appointment", back_populates="patient", cascade="all, delete-orphan")
    consultations = relationship("Consultation", back_populates="patient", cascade="all, delete-orphan")

class Appointment(Base):
    __tablename__ = 'appointments'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(DateTime, nullable=False, index=True)
    status = Column(String, default="Scheduled")
    doctor_name = Column(String, nullable=True) 
    
    doctor_id = Column(String, ForeignKey('doctors.id', ondelete='CASCADE'), nullable=False)
    patient_id = Column(String, ForeignKey('patients.id', ondelete='CASCADE'), nullable=False)
    
    doctor = relationship("Doctor", back_populates="appointments")
    patient = relationship("Patient", back_populates="appointments")

class Consultation(Base):
    __tablename__ = 'consultations'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(DateTime, default=datetime.utcnow, index=True)
    transcript = Column(Text, nullable=False)
    extracted_data = Column(JSONB, default=dict, server_default='{}') 
    
    patient_id = Column(String, ForeignKey('patients.id', ondelete='CASCADE'), nullable=False)
    patient = relationship("Patient", back_populates="consultations")