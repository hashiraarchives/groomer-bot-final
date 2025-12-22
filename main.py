import os
import json
import requests
import warnings
from fastapi import FastAPI, Request, Response, BackgroundTasks
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta

# Suppress the Google Deprecation Warning (It's just noise for now)
warnings.filterwarnings("ignore", category=FutureWarning)

# --- Configuration ---
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# !!! CRITICAL FIX: Using YOUR Railway Variable Name !!!
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON") 

app = FastAPI()

def log_message(message):
    print(f"[{datetime.now()}] - {message}", flush=True) # flush=True ensures logs appear instantly

# --- Google Calendar Setup ---
calendar_service = None
if GOOGLE_CREDS_JSON:
    try:
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_authorized_user_info(creds_info)
        calendar_service = build('calendar', 'v3', credentials=creds)
        log_message("SUCCESS: Google Calendar service loaded.")
    except Exception as e:
        log_message(f"ERROR: Calendar auth failed: {e}")
else:
    log_message("CRITICAL ERROR: GOOGLE_CREDENTIALS_JSON variable is missing in Railway!")

# --- Tools ---
def check_availability(date: str):
    if not calendar_service: return "Calendar unavailable."
    try:
        log_message(f"Tool Call: check_availability({date})")
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
        log_message(f"Tool Error: {e}")
        return "Error checking calendar."

def book_slot(date: str, time: str, service: str, name: str):
    if not calendar_service: return "Calendar unavailable."
    try:
        log_message(f"Tool Call: book_slot({date}, {time}, {service}, {name})")
        start_dt = datetime.strptime(f"{date} {time}", '%Y-%m-%d %H:%M')
        event = {
            'summary': f'{service} for {name}',
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
            'end': {'dateTime': (start_dt + timedelta(minutes=30)).isoformat(), 'timeZone': 'Asia/Singapore'},
        }
        calendar_service.events().insert(calendarId='primary', body=event).execute()
        return "Booking confirmed!"
    except Exception as e:
        log_message(f"Tool Error: {e}")
        return "Error booking slot."

# --- Gemini Setup ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name='gemini-1.5-flash',
        system_instruction=f"You are a receptionist. Today is {datetime.now().strftime('%Y-%m-%d')}.",
        tools=[check_availability, book_slot]
    )
    chat = model.start_chat(enable_automatic_function_calling=True)
    log_message("SUCCESS: Gemini AI loaded.")
else:
    log_message("CRITICAL ERROR: GEMINI_API_KEY is missing!")

# --- WhatsApp Logic ---
def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "text": {"body": body}})
    log_message(f"Sent reply to {to}: Status {resp.status_code}")

async def process_msg(payload):
    try:
        log_message("Processing payload...")
        change = payload['entry'][0]['changes'][0]['value']
        if 'messages' in change:
            msg = change['messages'][0]
            if msg['type'] == 'text':
                sender = msg['from']
                text = msg['text']['body']
                log_message(f"INCOMING MSG from {sender}: {text}")
                
                response = chat.send_message(text)
                reply = response.text
                
                log_message(f"GEMINI REPLY: {reply}")
                send_whatsapp(sender, reply)
            else:
                log_message(f"Ignored non-text message type: {msg['type']}")
    except Exception as e:
        log_message(f"ERROR inside process_msg: {e}")

# --- Webhooks ---
@app.get("/meta-webhook")
async def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        log_message("VERIFICATION SUCCESSFUL")
        return int(request.query_params.get("hub.challenge"))
    log_message("VERIFICATION FAILED")
    return Response(status_code=403)

@app.post("/meta-webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    # log_message(f"RAW POST RECEIVED: {body.decode('utf-8')}") # Uncomment if desperate
    
    data = json.loads(body)
    # Check if this is a message event
    if 'messages' in data.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {}):
        background_tasks.add_task(process_msg, data)
    
    return "OK"
