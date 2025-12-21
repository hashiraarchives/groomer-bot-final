"""
Pet Grooming WhatsApp Bot (Production Ready)
Stack: FastAPI + Twilio REST API + Gemini 1.5 Flash + Google Calendar
Features:
- Background Task Processing (Prevents Twilio Timeouts)
- Dynamic 'From' Number (Works for Sandbox & Paid)
- Secure Media Download (Fixes Voice/Image issues)
- ISO Date Parsing for Calendar
"""

import os
import json
import base64
import logging
import httpx
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import FastAPI, Request, Response, BackgroundTasks
from twilio.rest import Client
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIGURATION ---
# (Ensure these are set in Railway Variables)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
MODEL_NAME = "gemini-1.5-flash"
SGT = datetime.now().astimezone().tzinfo # Simple timezone hook

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GroomerBot")

# --- CLIENTS ---
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# =============================================================================
# 1. MEDIA HANDLER (Secure Download)
# =============================================================================
async def download_twilio_media(media_url: str) -> tuple[Optional[bytes], str]:
    if not media_url:
        return None, ""
    
    # CRITICAL: follow_redirects=True, NO Auth headers (if "Enforce Auth" disabled)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(media_url, timeout=30.0)
            if response.status_code != 200:
                logger.error(f"Media Download Failed: {response.status_code}")
                return None, ""
                
            content_type = response.headers.get("Content-Type", "")
            logger.info(f"Media Downloaded: {content_type} ({len(response.content)} bytes)")
            return response.content, content_type
        except Exception as e:
            logger.error(f"Download Error: {e}")
            return None, ""

# =============================================================================
# 2. CALENDAR MANAGER
# =============================================================================
class CalendarManager:
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        try:
            if GOOGLE_CREDENTIALS_JSON:
                info = json.loads(GOOGLE_CREDENTIALS_JSON)
                creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
                return build("calendar", "v3", credentials=creds)
        except Exception as e:
            logger.error(f"Auth Failed: {e}")
        return None

    def find_next_slot(self, duration_minutes: int = 90) -> str:
        if not self.service: return "Calendar Unavailable (Check Credentials)"
        
        now = datetime.now()
        start_search = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0)
        end_search = start_search + timedelta(days=5)

        try:
            events_result = self.service.events().list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMin=start_search.isoformat() + 'Z',
                timeMax=end_search.isoformat() + 'Z',
                singleEvents=True,
                orderBy='startTime'
            ).execute()
        except Exception as e:
            logger.error(f"Cal API Error: {e}")
            return "Could not check calendar."

        # Simple Availability Logic
        busy_times = [(e['start'].get('dateTime'), e['end'].get('dateTime')) for e in events_result.get('items', [])]
        
        curr = start_search
        while curr < end_search:
            # Skip nights (7PM - 10AM)
            if curr.hour >= 19:
                curr = (curr + timedelta(days=1)).replace(hour=10, minute=0)
                continue
                
            is_busy = False
            slot_end = curr + timedelta(minutes=duration_minutes)
            
            # Check overlap
            for b_start, b_end in busy_times:
                if b_start and b_end:
                    # Very basic ISO string comparison
                    if not (slot_end.isoformat() <= b_start or curr.isoformat() >= b_end):
                        is_busy = True
                        break
            
            if not is_busy:
                # Return BOTH human format and ISO for the AI
                return f"Available: {curr.strftime('%A, %d %B at %I:%M %p')}. (ISO: {curr.isoformat()})"
            
            curr += timedelta(minutes=30)
            
        return "No slots available next 5 days."

    def book_appointment(self, summary: str, time_str: str) -> str:
        if not self.service: return "Calendar Error."
        try:
            # Robust ISO Parsing
            if "T" in time_str:
                start_dt = datetime.fromisoformat(time_str)
            else:
                return "Error: Please provide ISO Format date."
                
            event = {
                'summary': summary,
                'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
                'end': {'dateTime': (start_dt + timedelta(minutes=90)).isoformat(), 'timeZone': 'Asia/Singapore'},
            }
            self.service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
            return "Booking Confirmed!"
        except Exception as e:
            logger.error(f"Booking Error: {e}")
            return "Failed to book slot."

calendar = CalendarManager()

# --- AI TOOLS ---
def check_availability(duration: int = 90):
    return calendar.find_next_slot(duration)

def book_slot(customer_info: str, iso_date: str):
    return calendar.book_appointment(customer_info, iso_date)

tools = [check_availability, book_slot]

# =============================================================================
# 3. FASTAPI APP
# =============================================================================
app = FastAPI()
sessions: Dict[str, List[Dict]] = {}

async def process_message(payload: dict):
    """
    Background Task: Handles the heavy lifting (Download -> AI -> Reply)
    """
    sender = payload.get("From")
    bot_number = payload.get("To") # Reply from WHO received it
    text_body = payload.get("Body", "")
    media_url = payload.get("MediaUrl0")
    media_type = payload.get("MediaContentType0", "")

    logger.info(f"Processing Msg from {sender}. Text: {text_body[:20]}...")

    # 1. Prepare Inputs
    user_parts = []
    if text_body:
        user_parts.append(text_body)

    if media_url:
        media_bytes, mime_type = await download_twilio_media(media_url)
        if media_bytes:
            b64_data = base64.b64encode(media_bytes).decode('utf-8')
            user_parts.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": b64_data
                }
            })
            if "audio" in mime_type:
                user_parts.append("This is a voice note. Listen and reply.")
            elif "image" in mime_type:
                user_parts.append("Analyze this image.")

    if not user_parts:
        return

    # 2. Get/Init History
    if sender not in sessions:
        sessions[sender] = []
    
    # 3. Call Gemini
    try:
        model = genai.GenerativeModel(
            model_name=MODEL_NAME,
            system_instruction="You are Bella, a grooming receptionist. 1. Use 'check_availability' to find slots. 2. When booking, MUST use ISO format (YYYY-MM-DDTHH:MM:SS) provided by the check tool. 3. Be friendly.",
            tools=tools
        )
        chat = model.start_chat(history=sessions[sender], enable_automatic_function_calling=True)
        
        response = chat.send_message(user_parts)
        ai_reply = response.text
        
        # Update History
        sessions[sender].append({"role": "user", "parts": [text_body or "[Media]"]})
        sessions[sender].append({"role": "model", "parts": [ai_reply]})

        # 4. SEND REPLY VIA REST API
        twilio_client.messages.create(
            from_=bot_number,
            to=sender,
            body=ai_reply
        )
        logger.info(f"Reply Sent to {sender}")

    except Exception as e:
        logger.error(f"AI/Twilio Error: {e}")
        # Optional: Send fallback message
        # twilio_client.messages.create(from_=bot_number, to=sender, body="Oops, I'm sleeping. Try again?")

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Entry Point: Accepts Twilio POST, spawns background task, returns 200 OK immediately.
    """
    form_data = await request.form()
    payload = dict(form_data)
    
    # Debug Log
    print(f"WEBHOOK RECEIVED: From {payload.get('From')} | Type: {payload.get('MediaContentType0') or 'Text'}")
    
    # Add to background queue (Non-blocking)
    background_tasks.add_task(process_message, payload)
    
    # Return 200 OK instantly so Twilio doesn't timeout
    return "OK"
