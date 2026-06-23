from database import Base, engine, SessionLocal, Doctor
from auth import get_password_hash

print("Dropping old database schema...")
# 1. Drop the old tables that are missing columns
Base.metadata.drop_all(bind=engine)

print("Rebuilding tables with new columns...")
# 2. Create fresh tables using the updated database.py
Base.metadata.create_all(bind=engine)

print("Seeding doctor profile...")
# 3. Re-create your doctor login since the tables were wiped
db = SessionLocal()
admin_doc = Doctor(
    username="dr_ishan", 
    hashed_password=get_password_hash("securepass123")
)
db.add(admin_doc)
db.commit()
db.close()

print("✅ Database successfully upgraded! You can now register patients.")