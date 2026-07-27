# JD TECH Attendance System

Employee attendance tracking application with Google Sheets sync.

## Setup

### 1. Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```
GOOGLE_SHEET_ID=your_google_sheet_id
GOOGLE_CREDENTIALS_JSON={"type":"service_account",...}  # full credentials.json content
SECRET_KEY=your_secret_key
```

### 2. Run Locally

```bash
pip install -r requirements.txt
python app.py
```

### 3. Deploy to Render / Koyeb / Railway

- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn app:app`
- Set the environment variables above in your hosting dashboard

## Environment Variables Reference

| Variable | Description |
|---|---|
| `GOOGLE_SHEET_ID` | Google Sheets spreadsheet ID |
| `GOOGLE_CREDENTIALS_JSON` | Full JSON content of Google service account credentials |
| `SECRET_KEY` | Flask session secret key (optional, has default) |
