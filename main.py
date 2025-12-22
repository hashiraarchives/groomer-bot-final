import os
import json
import requests
from fastapi import FastAPI, Request, Response, BackgroundTasks
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta

# --- Configuration ---
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CALENDAR_CREDENTIALS")

app = FastAPI()

def log_message(message):
    print(f"[{datetime.now()}] - {message}")

# --- Google Calendar Setup ---
calendar_service = None
if GOOGLE_CREDS_JSON:
    try:
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_authorized_user_info(creds_info)
        calendar_service = build('calendar', 'v3', credentials=creds)
        log_message("Google Calendar service loaded successfully.")
    except Exception as e:
        log_message(f"WARNING: Calendar auth failed: {e}")
else:
    log_message("WARNING: GOOGLE_CALENDAR_CREDENTIALS variable is missing!")

# --- Tools ---
def check_availability(date: str):
    """Checks available 30-minute slots on a given date (YYYY-MM-DD)."""
    if not calendar_service: return "Calendar unavailable."
    try:
        log_message(f"Checking date: {date}")
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
        return f"Error checking calendar: {str(e)}"

def book_slot(date: str, time: str, service: str, name: str):
    """Books a slot. Date: YYYY-MM-DD, Time: HH:MM."""
    if not calendar_service: return "Calendar unavailable."
    try:
        log_message(f"Booking {service} for {name} on {date} at {time}")
        start_dt = datetime.strptime(f"{date} {time}", '%Y-%m-%d %H:%M')
        event = {
            'summary': f'{service} for {name}',
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
            'end': {'dateTime': (start_dt + timedelta(minutes=30)).isoformat(), 'timeZone': 'Asia/Singapore'},
        }
        calendar_service.events().insert(calendarId='primary', body=event).execute()
        return "Booking confirmed!"
    except Exception as e:
        return f"Error booking: {str(e)}"

# --- Gemini Setup ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name='gemini-1.5-flash',
        system_instruction=f"You are a receptionist. Today is {datetime.now().strftime('%Y-%m-%d')}.",
        tools=[check_availability, book_slot]
    )
    chat = model.start_chat(enable_automatic_function_calling=True)
else:
    log_message("WARNING: GEMINI_API_KEY is missing!")

# --- WhatsApp Logic ---
def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "text": {"body": body}})

async def process_msg(payload):
    try:
        change = payload['entry'][0]['changes'][0]['value']
        if 'messages' in change:
            msg = change['messages'][0]
            if msg['type'] == 'text':
                sender = msg['from']
                text = msg['text']['body']
                log_message(f"User ({sender}): {text}")
                
                response = chat.send_message(text)
                reply = response.text
                
                log_message(f"Bot: {reply}")
                send_whatsapp(sender, reply)
    except Exception as e:
        log_message(f"Error processing: {e}")

# --- Webhooks ---
@app.get("/meta-webhook")
async def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(request.query_params.get("hub.challenge"))
    return Response(status_code=403)

@app.post("/meta-webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    background_tasks.add_task(process_msg, data)
    return "OK"
