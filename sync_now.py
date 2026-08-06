import os
from app import app
from sheets_db import pull_from_sheets, Employee, Attendance, Location, PO, Holiday

print("Starting Google Sheets -> SQLite full sync...")
with app.app_context():
    pull_from_sheets(app)

    print("\n--- SYNC COMPLETE ---")
    print(f"Employees in SQLite:  {Employee.query.count()}")
    print(f"Attendance in SQLite: {Attendance.query.count()}")
    print(f"Locations in SQLite:  {Location.query.count()}")
    print(f"POs in SQLite:        {PO.query.count()}")
    print(f"Holidays in SQLite:   {Holiday.query.count()}")
