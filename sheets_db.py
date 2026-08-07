"""
Google Sheets Database Layer with local SQLite caching.
Implements the same Flask-SQLAlchemy models and exposes the same public API
as the SQL Server/MySQL version, syncing data with Google Sheets in the background.
"""

import os, time, threading
from datetime import date, datetime
from flask import Flask

# Enforce India Standard Time (IST)
os.environ['TZ'] = 'Asia/Kolkata'
if hasattr(time, 'tzset'):
    time.tzset()
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
import gspread
from google.oauth2.service_account import Credentials

db = SQLAlchemy()

# ── Configuration Variables ──────────────────────────────────────────────────
GOOGLE_SHEET_ID = None
GOOGLE_SERVICE_ACCOUNT_FILE = None
syncing_from_sheets = False
modified_tables = set()
google_auth_working = True

# ── Models ───────────────────────────────────────────────────────────────────

class Employee(db.Model):
    __tablename__ = 'employees'
    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(20), unique=True, nullable=False)
    password    = db.Column(db.String(256), nullable=True)
    role        = db.Column(db.String(20), default='employee')
    full_name   = db.Column(db.String(120), nullable=False)
    department  = db.Column(db.String(80), nullable=False)
    position    = db.Column(db.String(80))
    email       = db.Column(db.String(120))
    phone       = db.Column(db.String(20))
    join_date   = db.Column(db.Date)
    active      = db.Column(db.Boolean, default=True)
    reporting_to = db.Column(db.String(120), nullable=True)
    attendance  = db.relationship('Attendance', backref='employee', lazy='dynamic')

class Attendance(db.Model):
    __tablename__ = 'attendance'
    __table_args__ = (
        db.UniqueConstraint('employee_id', 'date', name='uq_emp_date'),
    )
    id               = db.Column(db.Integer, primary_key=True)
    employee_id      = db.Column(db.String(20), db.ForeignKey('employees.employee_id'), nullable=False)
    date             = db.Column(db.Date, nullable=False)
    status           = db.Column(db.String(20), nullable=False)
    check_in         = db.Column(db.String(10))
    check_out        = db.Column(db.String(10))
    notes            = db.Column(db.String(255))
    marked_by        = db.Column(db.String(80))
    location         = db.Column(db.String(100))
    po_number        = db.Column(db.String(80))
    reporting_person = db.Column(db.String(120))
    zone             = db.Column(db.String(80))
    po_worked_days   = db.Column(db.Integer, nullable=True)
    time_worked      = db.Column(db.String(20), nullable=True)
    work_update      = db.Column(db.Text, nullable=True)
    visit_type       = db.Column(db.String(80), nullable=True)

class Location(db.Model):
    __tablename__ = 'locations'
    id            = db.Column(db.Integer, primary_key=True)
    customer_id   = db.Column(db.Integer, nullable=True)
    customer_name = db.Column(db.String(120), nullable=True)
    zone          = db.Column(db.String(255), nullable=True)
    latitude      = db.Column(db.Float, nullable=True)
    longitude     = db.Column(db.Float, nullable=True)
    created_at    = db.Column(db.String(80), nullable=True)
    added_by      = db.Column(db.String(120), nullable=True)

class PO(db.Model):
    __tablename__ = 'pos'
    id               = db.Column(db.Integer, primary_key=True)
    s_no             = db.Column(db.Integer, nullable=True)
    customer_name    = db.Column(db.String(120), nullable=False)
    zone             = db.Column(db.String(80), nullable=False)
    po_number        = db.Column(db.String(80), nullable=True)
    days             = db.Column(db.String(20), nullable=True)
    worked_days      = db.Column(db.Integer, nullable=True)
    reporting_person = db.Column(db.String(120), nullable=True)
    added_by         = db.Column(db.String(120), nullable=True)

class Holiday(db.Model):
    __tablename__ = 'holidays'
    id          = db.Column(db.Integer, primary_key=True)
    date        = db.Column(db.Date, nullable=False, unique=True)
    name        = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    added_by    = db.Column(db.String(120), nullable=True)

# Mapping table name to model and fields
TABLE_TO_MODEL = {
    'employees': Employee,
    'attendance': Attendance,
    'locations': Location,
    'pos': PO,
    'holidays': Holiday
}

TABLE_HEADERS = {
    'employees': ['id', 'employee_id', 'password', 'role', 'full_name', 'department', 'position', 'email', 'phone', 'join_date', 'active', 'reporting_to'],
    'attendance': ['id', 'employee_id', 'date', 'status', 'check_in', 'check_out', 'time_worked', 'notes', 'marked_by', 'location', 'po_number', 'reporting_person', 'zone', 'po_worked_days', 'work_update', 'visit_type'],
    'locations': ['Customer ID', 'Customer Name', 'Zone', 'Latitude', 'Longitude', 'Created At', 'Added By'],
    'pos': ['S.no', 'Customer Name', 'Zone', 'Po Number', 'Days', 'Worked Days', 'Reporting Person', 'Added By'],
    'holidays': ['Date', 'Holiday Name', 'Description', 'Added By']
}

import re

def parse_date_str(val):
    if not val:
        return None
    val = str(val).strip()
    for fmt in ('%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(val)
    except ValueError:
        return None

def parse_coordinate_str(val):
    if val in ("", None):
        return None
    val_str = str(val).strip().rstrip(',')
    try:
        return float(val_str)
    except ValueError:
        pass

    # Check for Degrees Minutes Seconds (DMS) format e.g. 13°10'43.0"N or 80°18'26.1"E
    dms_match = re.search(r'(\d+)\s*°\s*(\d+)\s*[\'′]\s*(\d+(?:\.\d+)?)\s*["″]?\s*([NSEWnsew])?', val_str)
    if dms_match:
        try:
            deg = float(dms_match.group(1))
            minute = float(dms_match.group(2))
            sec = float(dms_match.group(3))
            direction = dms_match.group(4)
            decimal = deg + (minute / 60.0) + (sec / 3600.0)
            if direction and direction.upper() in ('S', 'W'):
                decimal = -abs(decimal)
            return round(decimal, 7)
        except Exception:
            pass

    match = re.search(r'([-+]?\d+(?:\.\d+)?)\s*°?\s*([NSEWnsew])?', val_str)
    if match:
        try:
            num = float(match.group(1))
            dir_char = match.group(2)
            if dir_char and dir_char.upper() in ('S', 'W'):
                num = -abs(num)
            return num
        except ValueError:
            pass
    return None

def serialize_attendance(r):
    if not r:
        return ""
    parts = []
    if r.check_in:
        parts.append(f"In: {r.check_in}")
    if r.check_out:
        parts.append(f"Out: {r.check_out}")
    if r.location:
        parts.append(f"Loc: {r.location}")
    if r.po_number:
        parts.append(f"PO: {r.po_number}")
    if r.zone:
        parts.append(f"Zone: {r.zone}")
    if r.reporting_person:
        parts.append(f"RP: {r.reporting_person}")
    if r.notes:
        parts.append(f"Notes: {r.notes}")
    
    if parts:
        return f"{r.status} ({', '.join(parts)})"
    return r.status

def deserialize_attendance(val_str):
    val_str = str(val_str).strip()
    if not val_str:
        return None
    
    match = re.match(r'^([^(]+)(?:\((.*)\))?$', val_str)
    if not match:
        return {
            'status': val_str,
            'check_in': None,
            'check_out': None,
            'location': None,
            'po_number': None,
            'zone': None,
            'reporting_person': None,
            'notes': None
        }
    
    status = match.group(1).strip()
    details_str = match.group(2)
    
    details = {
        'status': status,
        'check_in': None,
        'check_out': None,
        'location': None,
        'po_number': None,
        'zone': None,
        'reporting_person': None,
        'notes': None
    }
    
    if details_str:
        pattern = r'(In|Out|Loc|PO|Zone|RP|Notes):\s*(.*?)(?=\s*(?:In|Out|Loc|PO|Zone|RP|Notes):|$)'
        matches = re.findall(pattern, details_str)
        for key, val in matches:
            val = val.strip().rstrip(',')
            if key == 'In':
                details['check_in'] = val
            elif key == 'Out':
                details['check_out'] = val
            elif key == 'Loc':
                details['location'] = val
            elif key == 'PO':
                details['po_number'] = val
            elif key == 'Zone':
                details['zone'] = val
            elif key == 'RP':
                details['reporting_person'] = val
            elif key == 'Notes':
                details['notes'] = val
    return details

def calculate_time_worked(check_in, check_out):
    if not check_in or not check_out:
        return None
    try:
        ci_h, ci_m = map(int, check_in.split(':'))
        co_h, co_m = map(int, check_out.split(':'))
        diff_mins = (co_h * 60 + co_m) - (ci_h * 60 + ci_m)
        if diff_mins < 0:
            diff_mins += 24 * 60
        h = diff_mins // 60
        m = diff_mins % 60
        return f"{h}h {m}m"
    except Exception:
        return None

def recalculate_po_worked_days_local(employee_id):
    SITE_SUPPORT_EXCLUSION_START_DATE = date(2026, 8, 8)
    records = Attendance.query.filter_by(employee_id=employee_id).order_by(Attendance.date.asc()).all()
    po_counts = {}
    for r in records:
        if r.po_number:
            po = r.po_number.strip()
            if not po:
                r.po_worked_days = None
                continue
            visit = (r.visit_type or '').strip().lower()
            if r.date and r.date >= SITE_SUPPORT_EXCLUSION_START_DATE:
                if visit in ('site support', 'site-support') or ('site' in visit and 'support' in visit):
                    r.po_worked_days = None
                    continue
            if r.status in ['Present', 'Half Day']:
                po_counts[po] = po_counts.get(po, 0) + 1
                r.po_worked_days = po_counts[po]
            else:
                r.po_worked_days = None
        else:
            r.po_worked_days = None
    db.session.commit()

from sqlalchemy import func

def recalculate_all_pos_total_worked_days():
    """
    Calculates total days worked (Present / Half Day) across all employees for each PO object
    and updates PO.worked_days in SQLite. Excludes 'Site Support' visit types from tomorrow (2026-08-08) onwards.
    """
    SITE_SUPPORT_EXCLUSION_START_DATE = date(2026, 8, 8)
    try:
        all_pos = PO.query.all()
        has_changes = False
        for po_obj in all_pos:
            if po_obj.po_number and po_obj.po_number.strip():
                po_clean = po_obj.po_number.strip()
                records = Attendance.query.filter(
                    func.lower(func.trim(Attendance.po_number)) == po_clean.lower(),
                    Attendance.status.in_(['Present', 'Half Day'])
                ).all()
                
                valid_count = 0
                for r in records:
                    v = (r.visit_type or '').strip().lower()
                    if r.date and r.date >= SITE_SUPPORT_EXCLUSION_START_DATE:
                        if v in ('site support', 'site-support') or ('site' in v and 'support' in v):
                            continue
                    valid_count += 1
                if po_obj.worked_days != valid_count:
                    po_obj.worked_days = valid_count
                    has_changes = True
            elif po_obj.worked_days != 0 and po_obj.worked_days is not None:
                po_obj.worked_days = 0
                has_changes = True
        if has_changes:
            db.session.commit()
    except Exception as e:
        print(f"Error recalculating total worked days for POs: {e}")
        db.session.rollback()

ATTENDANCE_SHEET_HEADERS = [
    'Employee ID', 'Full Name', 'Department', 'Status',
    'Clock In', 'Clock Out', 'Time In Plant', 'Location', 'Zone',
    'PO Number', 'Reporting Person', 'Days on PO', 'Notes', 'Work Update'
]

def sync_attendance_to_sheet(worksheet):
    """Push all attendance records to Google Sheets grouped date-wise."""
    try:
        employees_map = {str(e.employee_id).strip(): e for e in Employee.query.all()}
        records = (
            db.session.query(Attendance)
            .order_by(Attendance.date.asc(), Attendance.employee_id.asc())
            .all()
        )
        if not records:
            print("Safety Guard: Local attendance table has 0 records. Skipping destructive Google Sheets clear/update.")
            return

        values = [ATTENDANCE_SHEET_HEADERS]
        current_date = None
        for att in records:
            if att.date != current_date:
                current_date = att.date
                date_str = att.date.strftime('%d-%m-%Y') if att.date else ''
                values.append([date_str] + [''] * (len(ATTENDANCE_SHEET_HEADERS) - 1))
            
            clean_emp_id = str(att.employee_id).strip() if att.employee_id else ''
            emp_obj = employees_map.get(clean_emp_id)

            values.append([
                clean_emp_id,
                emp_obj.full_name if (emp_obj and emp_obj.full_name) else '',
                emp_obj.department if (emp_obj and emp_obj.department) else '',
                att.status or '',
                att.check_in or '',
                att.check_out or '',
                att.time_worked or '',
                att.location or '',
                att.zone or '',
                att.po_number or '',
                att.reporting_person or '',
                att.po_worked_days or '',
                att.notes or '',
                att.work_update or ''
            ])
        for attempt in range(3):
            try:
                worksheet.clear()
                worksheet.update('A1', values)
                print(f"Successfully synced {len(records)} attendance records grouped date-wise to Google Sheets.")
                break
            except Exception as ex:
                if attempt < 2:
                    print(f"Sync attendance attempt {attempt + 1} failed ({ex}). Retrying in 2 seconds...")
                    time.sleep(2)
                else:
                    raise ex
    except Exception as e:
        print(f"Error syncing attendance to Google Sheets: {e}")

# Keep old name as alias for any legacy references
def sync_attendance_to_sheet_day_wise(worksheet):
    sync_attendance_to_sheet(worksheet)


# ── Local .env Parser ────────────────────────────────────────────────────────

def load_dotenv():
    env_path = '.env'
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip()
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    os.environ[key] = val

# ── Google Sheets API Helper ─────────────────────────────────────────────────

def get_gspread_client():
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    try:
        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON') or os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON') or os.environ.get('GOOGLE_CREDENTIALS')
        if creds_json and creds_json.strip():
            import json
            info = json.loads(creds_json.strip())
            credentials = Credentials.from_service_account_info(info, scopes=scopes)
            return gspread.authorize(credentials)
        elif GOOGLE_SHEET_ID and os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
            credentials = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes)
            return gspread.authorize(credentials)
        else:
            print("Google Sheets credentials or GOOGLE_SHEET_ID not configured.")
            return None
    except Exception as e:
        print(f"Authentication with Google API failed: {e}")
        return None

def get_or_create_worksheet(spreadsheet, title, headers):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows="100", cols=str(len(headers)))
        worksheet.append_row(headers)
        return worksheet

def get_sheet_row_vals(r, tablename, headers):
    if tablename == 'locations':
        return [
            r.customer_id if r.customer_id is not None else "",
            r.customer_name if r.customer_name is not None else "",
            r.zone if r.zone is not None else "",
            r.latitude if r.latitude is not None else "",
            r.longitude if r.longitude is not None else "",
            r.created_at if r.created_at is not None else "",
            r.added_by if r.added_by is not None else ""
        ]
    elif tablename == 'pos':
        return [
            r.s_no if r.s_no is not None else "",
            r.customer_name if r.customer_name is not None else "",
            r.zone if r.zone is not None else "",
            r.po_number if r.po_number is not None else "",
            r.days if r.days is not None else "",
            r.worked_days if r.worked_days is not None else 0,
            r.reporting_person if r.reporting_person is not None else "",
            r.added_by if r.added_by is not None else ""
        ]
    elif tablename == 'holidays':
        return [
            r.date.isoformat() if r.date else "",
            r.name if r.name is not None else "",
            r.description if r.description is not None else "",
            r.added_by if r.added_by is not None else ""
        ]
    else:
        row_vals = []
        for h in headers:
            val = getattr(r, h)
            if isinstance(val, (date, datetime)):
                val = val.isoformat()
            elif isinstance(val, bool):
                val = str(val)
            elif val is None:
                val = ""
            else:
                val = str(val)
            row_vals.append(val)
        return row_vals

def sync_table_to_sheet(worksheet, model, headers):
    """Pushes a local SQLite table's content to the given Google Sheets worksheet."""
    try:
        rows = model.query.order_by(model.id).all()
        if not rows:
            print(f"Safety Guard: Local table '{model.__tablename__}' has 0 rows. Skipping Google Sheets clear/update.")
            return

        values = [headers]
        for r in rows:
            row_vals = get_sheet_row_vals(r, model.__tablename__, headers)
            values.append(row_vals)
        worksheet.clear()
        worksheet.update("A1", values)
        print(f"Provisioned and synced table '{model.__tablename__}' to Google Sheets.")
    except Exception as e:
        print(f"Error provisioning table '{model.__tablename__}' to Google Sheets: {e}")

# ── Synchronization Functions ────────────────────────────────────────────────

def pull_from_sheets(app):
    """Fetch all records from Google Sheets and overwrite local SQLite cache."""
    global syncing_from_sheets
    
    client = get_gspread_client()
    if not client:
        print("Google Sheets credentials not configured or spreadsheet ID missing. Skipping pull.")
        return False, "Google Sheets credentials not configured or spreadsheet ID missing."
        
    try:
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        print(f"Error opening spreadsheet '{GOOGLE_SHEET_ID}': {e}. Skipping pull.")
        return False, f"Error opening spreadsheet: {e}"
        
    print("Synchronizing data from Google Sheets to local database...")
    syncing_from_sheets = True
    
    with app.app_context():
        try:
            for tablename, headers in TABLE_HEADERS.items():
                model = TABLE_TO_MODEL[tablename]
                
                if tablename == 'attendance':
                    try:
                        worksheet = spreadsheet.worksheet('attendance')
                        records = worksheet.get_all_records()
                    except gspread.exceptions.WorksheetNotFound:
                        worksheet = get_or_create_worksheet(spreadsheet, 'attendance', ATTENDANCE_SHEET_HEADERS)
                        sync_attendance_to_sheet(worksheet)
                        continue
                    except Exception as e:
                        print(f"Error reading worksheet 'attendance': {e}")
                        continue

                    if not records:
                        print("Safety Guard: Google Sheets 'attendance' tab returned 0 records. Preserving local SQLite cache.")
                        continue

                    # Detect format: date-wise, flat rows, or legacy day-wise
                    first_row_keys = list(records[0].keys()) if records else []
                    is_attendance_sheet = 'Clock In' in first_row_keys or 'Date' in first_row_keys or 'Employee ID' in first_row_keys

                    if is_attendance_sheet:
                        current_date_obj = None
                        for record in records:
                            col_a = str(record.get('Employee ID') or record.get('employee_id') or '').strip()
                            col_b = str(record.get('Full Name') or record.get('full_name') or '').strip()

                            if not col_a:
                                continue

                            # Check if col_a is a date header row (e.g. '24-07-2026') and col_b is empty
                            if not col_b:
                                parsed_date = parse_date_str(col_a)
                                if parsed_date:
                                    current_date_obj = parsed_date
                                    continue

                            date_val = str(record.get('Date') or '').strip()
                            date_obj = parse_date_str(date_val) or current_date_obj
                            emp_id = col_a

                            if not emp_id or not date_obj:
                                continue

                            check_in  = str(record.get('Clock In') or '').strip() or None
                            check_out = str(record.get('Clock Out') or '').strip() or None
                            
                            existing = Attendance.query.filter_by(employee_id=emp_id, date=date_obj).first()
                            if existing:
                                existing.status = str(record.get('Status') or 'Present').strip()
                                existing.check_in = check_in or existing.check_in
                                existing.check_out = check_out or existing.check_out
                                existing.time_worked = str(record.get('Time In Plant') or '').strip() or calculate_time_worked(existing.check_in, existing.check_out)
                                existing.location = str(record.get('Location') or '').strip() or existing.location
                                existing.zone = str(record.get('Zone') or '').strip() or existing.zone
                                existing.po_number = str(record.get('PO Number') or '').strip() or existing.po_number
                                existing.reporting_person = str(record.get('Reporting Person') or '').strip() or existing.reporting_person
                                if (str(record.get('Days on PO') or record.get('po_worked_days') or '').strip()).isdigit():
                                    existing.po_worked_days = int(str(record.get('Days on PO') or record.get('po_worked_days') or '').strip())
                                if str(record.get('Notes') or '').strip():
                                    existing.notes = str(record.get('Notes') or '').strip()
                                if str(record.get('Work Update') or '').strip():
                                    existing.work_update = str(record.get('Work Update') or '').strip()
                            else:
                                db.session.add(Attendance(
                                    employee_id=emp_id,
                                    date=date_obj,
                                    status=str(record.get('Status') or 'Present').strip(),
                                    check_in=check_in,
                                    check_out=check_out,
                                    time_worked=str(record.get('Time In Plant') or '').strip() or calculate_time_worked(check_in, check_out),
                                    location=str(record.get('Location') or '').strip() or None,
                                    zone=str(record.get('Zone') or '').strip() or None,
                                    po_number=str(record.get('PO Number') or '').strip() or None,
                                    reporting_person=str(record.get('Reporting Person') or '').strip() or None,
                                    po_worked_days=int(str(record.get('Days on PO') or record.get('po_worked_days') or '').strip()) if (str(record.get('Days on PO') or record.get('po_worked_days') or '').strip()).isdigit() else None,
                                    notes=str(record.get('Notes') or '').strip() or None,
                                    work_update=str(record.get('Work Update') or '').strip() or None,
                                    marked_by='Google Sheets Sync'
                                ))
                        continue
                    else:
                        # Legacy day-wise format: convert to flat
                        for record in records:
                            emp_id = str(record.get('Employee ID') or record.get('employee_id') or '').strip()
                            if not emp_id:
                                continue
                            for key, val in record.items():
                                if key in ['Employee ID','Full Name','Department','employee_id','full_name','department','id','Id','ID']:
                                    continue
                                try:
                                    date_obj = date.fromisoformat(str(key).strip())
                                except ValueError:
                                    continue
                                val_str = str(val).strip()
                                if val_str and val_str != '—':
                                    details = deserialize_attendance(val_str)
                                    if details:
                                        check_in  = details['check_in']
                                        check_out = details['check_out']
                                        existing = Attendance.query.filter_by(employee_id=emp_id, date=date_obj).first()
                                        if existing:
                                            existing.status = details['status']
                                            existing.check_in = check_in or existing.check_in
                                            existing.check_out = check_out or existing.check_out
                                            existing.time_worked = calculate_time_worked(existing.check_in, existing.check_out)
                                            existing.location = details['location'] or existing.location
                                            existing.po_number = details['po_number'] or existing.po_number
                                            existing.zone = details['zone'] or existing.zone
                                            existing.reporting_person = details['reporting_person'] or existing.reporting_person
                                            existing.notes = details['notes'] or existing.notes
                                        else:
                                            db.session.add(Attendance(
                                                employee_id=emp_id,
                                                date=date_obj,
                                                status=details['status'],
                                                check_in=check_in,
                                                check_out=check_out,
                                                time_worked=calculate_time_worked(check_in, check_out),
                                                location=details['location'],
                                                po_number=details['po_number'],
                                                zone=details['zone'],
                                                reporting_person=details['reporting_person'],
                                                notes=details['notes'],
                                                marked_by='Google Sheets Sync'
                                            ))
                    continue

                try:
                    worksheet = spreadsheet.worksheet(tablename)
                    records = worksheet.get_all_records()
                except gspread.exceptions.WorksheetNotFound:
                    # Create the worksheet
                    worksheet = get_or_create_worksheet(spreadsheet, tablename, headers)
                    # Push local cache data to the newly created sheet
                    sync_table_to_sheet(worksheet, model, headers)
                    continue
                except Exception as e:
                    print(f"Error reading worksheet '{tablename}': {e}")
                    continue
                
                if not records:
                    print(f"Safety Guard: Google Sheets '{tablename}' worksheet returned 0 records. Preserving local SQLite cache.")
                    continue

                # Insert or update rows (UPSERT)
                if tablename == 'locations':
                    for record in records:
                        cust_id = record.get('Customer ID')
                        cust_name = record.get('Customer Name')
                        zone = record.get('Zone')
                        latitude = record.get('Latitude')
                        longitude = record.get('Longitude')
                        created_at = record.get('Created At')
                        added_by = record.get('Added By') or record.get('added_by')
                        
                        if not cust_name:
                            continue
                            
                        try:
                            c_id = int(cust_id) if cust_id not in ("", None) else None
                        except ValueError:
                            c_id = None
                            
                        lat = parse_coordinate_str(latitude)
                        lon = parse_coordinate_str(longitude)

                        c_name_clean = str(cust_name).strip()
                        existing_loc = Location.query.filter_by(customer_name=c_name_clean).first()
                        if existing_loc:
                            if c_id is not None: existing_loc.customer_id = c_id
                            existing_loc.zone = str(zone).strip() if zone not in ("", None) else existing_loc.zone
                            if lat is not None: existing_loc.latitude = lat
                            if lon is not None: existing_loc.longitude = lon
                            if created_at not in ("", None): existing_loc.created_at = str(created_at).strip()
                            if added_by not in ("", None): existing_loc.added_by = str(added_by).strip()
                        else:
                            db.session.add(Location(
                                customer_id=c_id,
                                customer_name=c_name_clean,
                                zone=str(zone).strip() if zone not in ("", None) else "",
                                latitude=lat,
                                longitude=lon,
                                created_at=str(created_at).strip() if created_at not in ("", None) else "",
                                added_by=str(added_by).strip() if added_by not in ("", None) else None
                            ))
                elif tablename == 'pos':
                    for record in records:
                        s_no = record.get('S.no')
                        cust_name = record.get('Customer Name')
                        zone = record.get('Zone')
                        po_num = record.get('Po Number')
                        days = record.get('Days')
                        worked_days_raw = record.get('Worked Days') or record.get('Worked days') or record.get('worked_days')
                        rp = record.get('Reporting Person')
                        added_by = record.get('Added By') or record.get('added_by')
                        
                        if not cust_name:
                            continue
                            
                        try:
                            sno = int(s_no) if s_no not in ("", None) else None
                        except ValueError:
                            sno = None

                        try:
                            w_days = int(worked_days_raw) if worked_days_raw not in ("", None) else None
                        except (ValueError, TypeError):
                            w_days = None

                        c_name_clean = str(cust_name).strip()
                        po_num_clean = str(po_num).strip() if po_num not in ("", None) else ""
                        existing_po = PO.query.filter_by(customer_name=c_name_clean, po_number=po_num_clean).first()
                        if existing_po:
                            if sno is not None: existing_po.s_no = sno
                            existing_po.zone = str(zone).strip() if zone not in ("", None) else existing_po.zone
                            existing_po.days = str(days).strip() if days not in ("", None) else existing_po.days
                            if w_days is not None: existing_po.worked_days = w_days
                            existing_po.reporting_person = str(rp).strip() if rp not in ("", None) else existing_po.reporting_person
                            if added_by not in ("", None): existing_po.added_by = str(added_by).strip()
                        else:
                            db.session.add(PO(
                                s_no=sno,
                                customer_name=c_name_clean,
                                zone=str(zone).strip() if zone not in ("", None) else "",
                                po_number=po_num_clean,
                                days=str(days).strip() if days not in ("", None) else "",
                                worked_days=w_days,
                                reporting_person=str(rp).strip() if rp not in ("", None) else "",
                                added_by=str(added_by).strip() if added_by not in ("", None) else None
                            ))
                elif tablename == 'holidays':
                    for record in records:
                        rec_clean = {str(k).strip().lower(): v for k, v in record.items()}
                        h_date_raw = rec_clean.get('date') or rec_clean.get('holiday date')
                        h_name = rec_clean.get('holiday name') or rec_clean.get('name') or rec_clean.get('holiday')
                        h_desc = rec_clean.get('description') or rec_clean.get('desc')
                        h_added_by = rec_clean.get('added by') or rec_clean.get('added_by')

                        h_date = parse_date_str(h_date_raw)
                        if not h_date or not h_name:
                            continue

                        existing_h = Holiday.query.filter_by(date=h_date).first()
                        if existing_h:
                            existing_h.name = str(h_name).strip()
                            if h_desc not in ("", None): existing_h.description = str(h_desc).strip()
                            if h_added_by not in ("", None): existing_h.added_by = str(h_added_by).strip()
                        else:
                            db.session.add(Holiday(
                                date=h_date,
                                name=str(h_name).strip(),
                                description=str(h_desc).strip() if h_desc not in ("", None) else None,
                                added_by=str(h_added_by).strip() if h_added_by not in ("", None) else None
                            ))
                elif tablename == 'employees':
                    seen_emp_ids = set()
                    for record in records:
                        rec_clean = {str(k).strip().lower(): v for k, v in record.items()}

                        emp_id = str(rec_clean.get('employee_id') or rec_clean.get('employee id') or rec_clean.get('emp id') or rec_clean.get('id') or '').strip()
                        if not emp_id or emp_id in seen_emp_ids:
                            continue
                        seen_emp_ids.add(emp_id)

                        full_name = str(rec_clean.get('full_name') or rec_clean.get('full name') or rec_clean.get('name') or '').strip()
                        dept = str(rec_clean.get('department') or rec_clean.get('dept') or '').strip()
                        role = str(rec_clean.get('role') or 'employee').strip().lower()
                        pw = str(rec_clean.get('password') or '').strip()

                        pos_title = str(rec_clean.get('position') or rec_clean.get('designation') or '').strip() or None
                        email = str(rec_clean.get('email') or '').strip() or None
                        phone = str(rec_clean.get('phone') or '').strip() or None
                        reporting_to = str(rec_clean.get('reporting_to') or rec_clean.get('reporting to') or rec_clean.get('manager') or '').strip() or None

                        jd_str = str(rec_clean.get('join_date') or rec_clean.get('join date') or '').strip()
                        try:
                            jd = date.fromisoformat(jd_str) if jd_str else None
                        except ValueError:
                            jd = None

                        active_raw = str(rec_clean.get('active') or 'true').strip().lower()
                        is_active = active_raw not in ('false', '0', 'no', 'disabled')

                        existing_emp = Employee.query.filter_by(employee_id=emp_id).first()
                        if existing_emp:
                            if full_name: existing_emp.full_name = full_name
                            if dept: existing_emp.department = dept
                            if role: existing_emp.role = role
                            if pw: existing_emp.password = pw
                            if pos_title: existing_emp.position = pos_title
                            if email: existing_emp.email = email
                            if phone: existing_emp.phone = phone
                            if reporting_to: existing_emp.reporting_to = reporting_to
                            if jd: existing_emp.join_date = jd
                            existing_emp.active = is_active
                        else:
                            if not pw:
                                pw = hashlib.sha256(emp_id.encode()).hexdigest()
                            db.session.add(Employee(
                                employee_id=emp_id,
                                password=pw,
                                role=role,
                                full_name=full_name,
                                department=dept,
                                position=pos_title,
                                email=email,
                                phone=phone,
                                join_date=jd,
                                active=is_active,
                                reporting_to=reporting_to
                            ))
                else:
                    for record in records:
                        kwargs = {}
                        for h in headers:
                            val = record.get(h)
                            if val == "":
                                val = None
                            else:
                                col_type = getattr(model, h).property.columns[0].type
                                if col_type.python_type == date:
                                    try:
                                        val = date.fromisoformat(str(val))
                                    except ValueError:
                                        val = None
                                elif col_type.python_type == datetime:
                                    try:
                                        val = datetime.fromisoformat(str(val))
                                    except ValueError:
                                        val = None
                                elif col_type.python_type == bool:
                                    val = str(val).lower() in ('true', '1')
                                elif col_type.python_type == int:
                                    try:
                                        val = int(val) if val is not None else None
                                    except ValueError:
                                        val = None
                                elif col_type.python_type == float:
                                    try:
                                        val = float(val) if val is not None else None
                                    except ValueError:
                                        val = None
                            kwargs[h] = val
                        
                        db.session.add(model(**kwargs))
            
            # Recalculate PO worked days for all employees in local SQLite cache
            try:
                all_emp_ids = [e.employee_id for e in Employee.query.all()]
                for eid in all_emp_ids:
                    recalculate_po_worked_days_local(eid)
                recalculate_all_pos_total_worked_days()
            except Exception as e:
                print(f"Error recalculating PO worked days during pull: {e}")
                
            db.session.commit()
            print("Successfully synchronized data from Google Sheets.")
            return True, "Successfully synchronized data from Google Sheets."
        except Exception as e:
            db.session.rollback()
            print(f"Error writing pulled data to SQLite: {e}")
            return False, f"Error writing pulled data to SQLite: {e}"
        finally:
            syncing_from_sheets = False

def sync_tables_worker(app, tables):
    """Worker that pushes SQLite data to Google Sheets."""
    global syncing_from_sheets
    syncing_from_sheets = True
    try:
        with app.app_context():
            client = get_gspread_client()
            if not client:
                return
                
            try:
                spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
            except Exception as e:
                print(f"Error connecting to spreadsheet during sync: {e}")
                return
                
            # Ensure total worked days for all POs are recalculated before pushing
            try:
                recalculate_all_pos_total_worked_days()
            except Exception as ex:
                print(f"Error updating PO worked days before sync: {ex}")
                
            for tablename in tables:
                if tablename == 'attendance':
                    try:
                        worksheet = get_or_create_worksheet(spreadsheet, 'attendance', ATTENDANCE_SHEET_HEADERS)
                        sync_attendance_to_sheet(worksheet)
                        print("Pushed flat-row attendance to Google Sheets.")
                    except Exception as e:
                        print(f"Error syncing attendance table to Google Sheets: {e}")
                    continue

                model = TABLE_TO_MODEL.get(tablename)
                if not model:
                    continue
                    
                try:
                    if tablename == 'employees':
                        rows = model.query.filter_by(active=True).order_by(model.id).all()
                    else:
                        rows = model.query.order_by(model.id).all()

                    if not rows:
                        print(f"Safety Guard: Skipping background sync for '{tablename}' because local table has 0 rows.")
                        continue

                    headers = TABLE_HEADERS[tablename]
                    values = [headers]
                    
                    for r in rows:
                        row_vals = get_sheet_row_vals(r, tablename, headers)
                        values.append(row_vals)
                    
                    worksheet = get_or_create_worksheet(spreadsheet, tablename, headers)
                    worksheet.clear()
                    worksheet.update("A1", values)
                    print(f"Pushed table '{tablename}' to Google Sheets.")
                except Exception as e:
                    print(f"Error syncing table '{tablename}' to Google Sheets: {e}")
    finally:
        syncing_from_sheets = False


def trigger_background_sync(tables):
    from flask import current_app
    app = current_app._get_current_object()
    thread = threading.Thread(target=sync_tables_worker, args=(app, list(tables)))
    thread.daemon = True
    thread.start()

def sync_bidirectional(app):
    """
    Performs full two-way synchronization:
    1. Pulls latest updates & deletions from Google Sheets into local SQLite database.
    2. Pushes local tables (attendance, employees, locations, pos, holidays) to Google Sheets.
    Returns (success_boolean, message_string).
    """
    pull_success, pull_msg = pull_from_sheets(app)
    if not pull_success:
        print(f"Sync pull warning: {pull_msg}")

    try:
        all_tables = ['employees', 'attendance', 'locations', 'pos', 'holidays']
        sync_tables_worker(app, all_tables)
        return True, "Successfully synchronized both ways with Google Sheets!"
    except Exception as e:
        return False, f"Error pushing data to Google Sheets: {e}"

# ── Event Listeners ──────────────────────────────────────────────────────────

@event.listens_for(db.session, 'after_flush')
def after_flush(session, flush_context):
    global syncing_from_sheets, modified_tables
    if syncing_from_sheets:
        return
        
    for obj in session.new | session.dirty | session.deleted:
        if hasattr(obj, '__tablename__'):
            modified_tables.add(obj.__tablename__)

@event.listens_for(db.session, 'after_commit')
def after_commit(session):
    global syncing_from_sheets, modified_tables
    if syncing_from_sheets or not GOOGLE_SHEET_ID:
        modified_tables.clear()
        return
        
    if modified_tables:
        trigger_background_sync(list(modified_tables))
        modified_tables.clear()

# ── App Configuration ────────────────────────────────────────────────────────

def configure_app(app):
    global GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE
    load_dotenv()
    
    GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')
    GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE', 'credentials.json')
    
    db_url = os.environ.get('DATABASE_URL')
    if db_url:
        if db_url.startswith('postgres://'):
            db_url = db_url.replace('postgres://', 'postgresql://', 1)
        app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    elif os.environ.get('NETLIFY') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME') or os.environ.get('LAMBDA_TASK_ROOT') or os.environ.get('VERCEL') or os.environ.get('RENDER') or os.environ.get('RAILWAY') or os.environ.get('KOYEB'):
        import tempfile
        db_path = os.path.join(tempfile.gettempdir(), 'attendance.db')
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    else:
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///attendance.db'
        
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db.init_app(app)
