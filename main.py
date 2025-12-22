import os
import json
import requests
from fastapi import FastAPI, Request, Response, BackgroundTasks
import google.genai as genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta

# --- Configuration ---
# Load secrets from Railway environment variables
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CALENDAR_CREDENTIALS")

# --- FastAPI App Initialization ---
app = FastAPI()

# --- Logging ---
def log_message(message):
    """Prints a message with a timestamp for easy debugging."""
    print(f"[{datetime.now()}] - {message}")

# --- Google Calendar Tools ---
try:
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_authorized_user_info(creds_info)
    calendar_service = build('calendar', 'v3', credentials=creds)
    log_message("Successfully loaded Google Calendar credentials.")
except Exception as e:
    log_message(f"ERROR: Failed to load Google Calendar credentials: {e}")
    calendar_service = None

def check_availability(date: str):
    """
    Checks available 30-minute slots on a given date.
    Date format should be 'YYYY-MM-DD'.
    """
    if not calendar_service:
        return "Error: Calendar service is not available."
    try:
        log_message(f"Checking availability for date: {date}")
        day = datetime.strptime(date, '%Y-%m-%d')
        start_time = day.replace(hour=9, minute=0, second=0, microsecond=0).isoformat() + 'Z'
        end_time = day.replace(hour=17, minute=0, second=0, microsecond=0).isoformat() + 'Z'

        events_result = calendar_service.events().list(
            calendarId='primary',
            timeMin=start_time,
            timeMax=end_time,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        busy_slots = events_result.get('items', [])

        busy_times = [(datetime.fromisoformat(e['start']['dateTime'][:-1]), datetime.fromisoformat(e['end']['dateTime'][:-1])) for e in busy_slots]

        available_slots = []
        current_time = day.replace(hour=9, minute=0)
        while current_time.hour < 17:
            is_busy = any(start < current_time.replace(tzinfo=None) < end for start, end in busy_times)
            if not is_busy:
                available_slots.append(current_time.strftime('%H:%M'))
            current_time += timedelta(minutes=30)
        
        log_message(f"Found available slots: {available_slots}")
        return f"Available slots on {date}: {', '.join(available_slots) if available_slots else 'None'}"
    except Exception as e:
        log_message(f"ERROR in check_availability: {e}")
        return "Sorry, I couldn't check the calendar. Please provide the date in YYYY-MM-DD format."

def book_slot(date: str, time: str, service: str, customer_name: str):
    """
    Books a 30-minute appointment.
    Date format: 'YYYY-MM-DD', Time format: 'HH:MM'.
    """
    if not calendar_service:
        return "Error: Calendar service is not available."
    try:
        log_message(f"Booking slot for {customer_name} on {date} at {time} for {service}")
        start_dt = datetime.strptime(f"{date} {time}", '%Y-%m-%d %H:%M')
        end_dt = start_dt + timedelta(minutes=30)

        event = {
            'summary': f'{service} for {customer_name}',
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
        }
        
        created_event = calendar_service.events().insert(calendarId='primary', body=event).execute()
        log_message(f"Successfully created event: {created_event.get('htmlLink')}")
        return f"Booking confirmed for {customer_name} on {date} at {time} for a {service}."
    except Exception as e:
        log_message(f"ERROR in book_slot: {e}")
        return "Sorry, I couldn't book the appointment. Please check the date and time format."

# --- Gemini AI Configuration ---
genai.configure(api_key=GEMINI_API_KEY)

# Define the system prompt and tools for the model
SYSTEM_INSTRUCTION = "You are a friendly and efficient receptionist for 'ABC Grooming'. Your goal is to help customers check appointment availability and book services. Use the available tools to interact with the Google Calendar. Today's date is " + datetime.now().strftime('%Y-%m-%d') + "."
model = genai.GenerativeModel(
    model_name='gemini-1.5-flash-latest',
    system_instruction=SYSTEM_INSTRUCTION,
    tools=[check_availability, book_slot]
)
# Start a new chat session
chat = model.start_chat(enable_automatic_function_calling=True)

# --- WhatsApp Messaging Functions ---
def send_whatsapp_message(to_number, message_text):
    """Sends a text message using the WhatsApp Cloud API."""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "text": {"body": message_text},
    }
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        log_message(f"Sent message to {to_number}. Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        log_message(f"ERROR sending message to {to_number}: {e}")

async def process_whatsapp_message(payload: dict):
    """Processes the incoming message, calls Gemini, and sends a reply."""
    try:
        # Extract relevant information
        value = payload['entry'][0]['changes'][0]['value']
        if 'messages' not in value:
            return

        message_data = value['messages'][0]
        from_number = message_data['from']
        
        if message_data['type'] != 'text':
            send_whatsapp_message(from_number, "I can only process text messages for now, sorry!")
            return

        user_message = message_data['text']['body']
        log_message(f"Received message from {from_number}: '{user_message}'")

        # Send message to Gemini and get response
        response = chat.send_message(user_message)
        
        # The new gemini-1.5-flash model with automatic function calling handles the loop.
        # The final response text will be in response.text
        bot_reply = response.text
        
        log_message(f"Gemini response for {from_number}: '{bot_reply}'")
        send_whatsapp_message(from_number, bot_reply)

    except Exception as e:
        log_message(f"ERROR in process_whatsapp_message: {e}")
        # Optionally send an error message to the user
        # send_whatsapp_message(from_number, "I'm having some trouble right now. Please try again later.")


# --- Webhook Endpoints ---
@app.get("/meta-webhook")
async def verify_webhook(request: Request):
    """Handles the webhook verification challenge from Meta."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        log_message("Webhook verification successful!")
        return Response(content=challenge, status_code=200)
    else:
        log_message("Webhook verification failed!")
        return Response(status_code=403)

@app.post("/meta-webhook")
async def message_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handles incoming messages from WhatsApp."""
    try:
        payload = await request.json()
        # Log the raw payload for intense debugging if needed
        # log_message(f"RAW WEBHOOK PAYLOAD: {payload}")
        
        # Ensure it's a message payload and not a status update
        if (payload.get('object') == 'whatsapp_business_account' and 
            'messages' in payload['entry'][0]['changes'][0]['value']):
            
            background_tasks.add_task(process_whatsapp_message, payload)
        
        return Response(status_code=200)
    except Exception as e:
        log_message(f"ERROR processing POST request: {e}")
        return Response(status_code=500)

@app.get("/")
async def root():
    return {"status": "ok"}

