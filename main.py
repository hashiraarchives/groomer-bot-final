import sys
# FORCE logs to appear immediately (Disable buffering)
sys.stdout.reconfigure(line_buffering=True)

import os
import json
import requests
import warnings
from fastapi import FastAPI, Request, Response, BackgroundTasks
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta

# Suppress warnings to keep logs clean
warnings.filterwarnings("ignore", category=FutureWarning)

# --- Configuration ---
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

app = FastAPI()

def log_message(message):
    """Prints with timestamp and ensures immediate output."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)

# --- Google Calendar Setup ---
calendar_service = None
if GOOGLE_CREDS_JSON:
    try:
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        # Check if we have the right keys before trying to auth
        if 'refresh_token' in creds_info:
            creds = Credentials.from_authorized_user_info(creds_info)
            calendar_service = build('calendar', 'v3', credentials=creds)
            log_message("SUCCESS: Google Calendar service loaded.")
        else:
            log_message("WARNING: Credentials JSON missing 'refresh_token'. Calendar disabled.")
    except Exception as e:
        log_message(f"ERROR: Calendar auth failed: {e}")
else:
    log_message("NOTICE: No Google Credentials found. Calendar tools disabled.")

# --- Tools ---
def check_availability(date: str):
    if not calendar_service: return "I cannot check the calendar right now."
    try:
        log_message(f"TOOL: Checking availability for {date}")
        day = datetime.strptime(date, '%Y-%m-%d')
        start_time = day.replace(hour=9, minute=0).isoformat() + 'Z'
        end_time = day.replace(hour=17, minute=0).isoformat() + 'Z'
        events = calendar_service.events().list(
            calendarId='primary', timeMin=start_time, timeMax=end_time, singleEvents=True, orderBy='startTime'
        ).execute().get('items', [])
        
        busy_times = [(datetime.fromisoformat(e['start']['dateTime'][:-1]), datetime.fromisoformat(e['end']['dateTime'][:-1])) for e in events]
        available = []
        curr = day.replace(hour=9, minute=0)
        while curr.hour < 17:
            if not any(start <= curr < end for start, end in busy_times):
                available.append(curr.strftime('%H:%M'))
            curr += timedelta(minutes=30)
        return f"Slots on {date}: {', '.join(available)}" if available else "No slots available."
    except Exception as e:
        log_message(f"TOOL ERROR: {e}")
        return "Error checking calendar."

def book_slot(date: str, time: str, service: str, name: str):
    if not calendar_service: return "I cannot book appointments right now."
    try:
        log_message(f"TOOL: Booking {service} for {name} on {date} at {time}")
        start_dt = datetime.strptime(f"{date} {time}", '%Y-%m-%d %H:%M')
        event = {
            'summary': f'{service} for {name}',
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
            'end': {'dateTime': (start_dt + timedelta(minutes=30)).isoformat(), 'timeZone': 'Asia/Singapore'},
        }
        calendar_service.events().insert(calendarId='primary', body=event).execute()
        return "Booking confirmed!"
    except Exception as e:
        log_message(f"TOOL ERROR: {e}")
        return "Error booking slot."

# --- Gemini Setup ---
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name='gemini-3-flash-preview',
            system_instruction=f"You are a receptionist. Today is {datetime.now().strftime('%Y-%m-%d')}. Keep replies short.",
            tools=[check_availability, book_slot]
        )
        chat = model.start_chat(enable_automatic_function_calling=True)
        log_message("SUCCESS: Gemini AI loaded.")
    except Exception as e:
        log_message(f"ERROR: Gemini Init Failed: {e}")
else:
    log_message("CRITICAL: GEMINI_API_KEY missing.")

# --- WhatsApp Logic ---
def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "text": {"body": body}})
        log_message(f"OUTGOING: Sent reply to {to} (Status: {resp.status_code})")
        if resp.status_code != 200:
            log_message(f"SEND ERROR: {resp.text}")
    except Exception as e:
        log_message(f"SEND EXCEPTION: {e}")

async def process_msg(payload):
    try:
        # Extract message safely
        entry = payload.get('entry', [{}])[0]
        change = entry.get('changes', [{}])[0].get('value', {})
        
        if 'messages' in change:
            msg = change['messages'][0]
            if msg['type'] == 'text':
                sender = msg['from']
                text = msg['text']['body']
                log_message(f"INCOMING: '{text}' from {sender}")
                
                # Generate AI Response
                response = chat.send_message(text)
                reply = response.text
                
                log_message(f"GEMINI: '{reply}'")
                send_whatsapp(sender, reply)
            else:
                log_message("Ignored non-text message.")
        elif 'statuses' in change:
            # Silence status updates (read receipts etc) to keep logs clean
            pass 
        else:
            log_message(f"Ignored unknown event: {list(change.keys())}")
            
    except Exception as e:
        log_message(f"PROCESS ERROR: {e}")

# --- Webhook Routes (The "Catch-All" Strategy) ---

@app.get("/meta-webhook")
@app.get("/webhook") # Listen on BOTH paths for verification
async def verify(request: Request):
    token = request.query_params.get("hub.verify_token")
    if token == VERIFY_TOKEN:
        log_message(f"VERIFICATION HIT on {request.url.path} - Success")
        return int(request.query_params.get("hub.challenge"))
    log_message(f"VERIFICATION HIT on {request.url.path} - FAILED (Token mismatch)")
    return Response(status_code=403)

@app.post("/meta-webhook")
@app.post("/webhook") # Listen on BOTH paths for messages
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.body()
        data = json.loads(body)
        
        # Log that we actually got something
        # log_message(f"WEBHOOK HIT on {request.url.path}") 
        
        background_tasks.add_task(process_msg, data)
        return "OK"
    except Exception as e:
        log_message(f"WEBHOOK CRITICAL ERROR: {e}")
        return Response(status_code=500)

@app.get("/")
async def root():
    return {"status": "Bot is Alive", "time": datetime.now().strftime('%H:%M:%S')}
