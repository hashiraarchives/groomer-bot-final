"""
Pet Grooming WhatsApp Bot (Final Fix Edition)
==============================================================
Stack: FastAPI + Twilio + Gemini 3 Flash + Google Calendar
Updates:
- ENABLED follow_redirects=True (Critical for Twilio Media)
- REMOVED Auth headers (Since "Enforce HTTP Auth" is disabled in Twilio)
- Full Debugging Logging enabled
"""

import os
import json
import logging
import base64
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Form
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

import google.generativeai as genai
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# =============================================================================
# CONFIGURATION
# =============================================================================

# API Keys & Secrets
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Model: Using the Preview of Gemini 3 Flash (Dec 2025)
MODEL_NAME = "gemini-3-flash-preview"

SGT = ZoneInfo("Asia/Singapore")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("BellaBot")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# =============================================================================
# MEDIA HANDLER (THE FIX)
# =============================================================================

async def download_twilio_media(media_url: str) -> tuple[bytes, str]:
    """
    CRITICAL FIX: 
    1. follows redirects (Twilio -> AWS S3).
    2. No Auth headers (prevents AWS S3 403 errors since you disabled Auth in Twilio).
    """
    if not media_url:
        return None, None

    # UPDATED CLIENT: follow_redirects=True
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            # UPDATED GET: Removed auth=(...) 
            response = await client.get(media_url, timeout=30.0)
            
            if response.status_code != 200:
                logger.error(f"Failed to download media. Status: {response.status_code}, URL: {media_url}")
                return None, None
                
            content_type = response.headers.get("Content-Type", "")
            logger.info(f"Successfully downloaded media. Type: {content_type}, Size: {len(response.content)} bytes")
            return response.content, content_type
            
        except Exception as e:
            logger.error(f"Download Error: {e}")
            return None, None

# =============================================================================
# GOOGLE CALENDAR & TOOLS
# =============================================================================

class CalendarManager:
    # (Standard robust calendar logic)
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if GOOGLE_CREDENTIALS_JSON:
            try:
                info = json.loads(GOOGLE_CREDENTIALS_JSON)
                creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
            except Exception as e:
                logger.error(f"Creds Error: {e}")
        elif os.path.exists("service_account.json"):
            creds = service_account.Credentials.from_service_account_file("service_account.json", scopes=self.SCOPES)
        
        if not creds:
            return None
        return build("calendar", "v3", credentials=creds)

    def find_next_slot(self, duration_minutes: int = 90) -> str:
        if not self.service: return "Calendar unavailable."
        now = datetime.now(SGT)
        start_search = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        end_search = start_search + timedelta(days=5)

        try:
            events_result = self.service.events().list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMin=start_search.isoformat(),
                timeMax=end_search.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
        except Exception as e:
            logger.error(f"Cal API Error: {e}")
            return "Could not check calendar."

        busy_times = [(e['start'].get('dateTime'), e['end'].get('dateTime')) for e in events_result.get('items', [])]

        curr = start_search
        while curr < end_search:
            if curr.hour >= 19:
                curr = (curr + timedelta(days=1)).replace(hour=10, minute=0)
                continue
            
            is_busy = False
            slot_end = curr + timedelta(minutes=duration_minutes)
            for b_start, b_end in busy_times:
                if b_start and b_end:
                    if not (slot_end.isoformat() <= b_start or curr.isoformat() >= b_end):
                        is_busy = True
                        break
            
            if not is_busy:
                return curr.strftime("%A, %d %B at %I:%M %p")
            curr += timedelta(minutes=30)
            
        return "No slots available next 5 days."

    def book_appointment(self, summary: str, time_str: str) -> str:
        if not self.service: return "Calendar Error."
        try:
            start_dt = datetime.fromisoformat(time_str) 
        except:
            return "Error: Please provide date in ISO format (YYYY-MM-DDTHH:MM:SS)"
            
        event = {
            'summary': summary,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
            'end': {'dateTime': (start_dt + timedelta(minutes=90)).isoformat(), 'timeZone': 'Asia/Singapore'},
        }
        try:
            self.service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
            return "Booked!"
        except Exception as e:
            logger.error(f"Booking Error: {e}")
            return "Failed to create event."

calendar = CalendarManager()

# --- GEMINI TOOLS ---
def check_availability(duration_minutes: int = 90):
    return calendar.find_next_slot(duration_minutes)

def save_dog_details(breed: str, size: str, coat: str, price: int):
    # Log this to console so we know Gemini saw it
    logger.info(f"DOG ANALYZED: Breed={breed}, Size={size}, Price=${price}")
    return "Details Saved."

def confirm_booking(customer_phone: str, date_iso: str, service: str):
    return calendar.book_appointment(f"{service} ({customer_phone})", date_iso)

tools = [check_availability, save_dog_details, confirm_booking]

# =============================================================================
# FASTAPI WEBHOOK
# =============================================================================

app = FastAPI()

# Session store 
sessions: Dict[str, List[Dict]] = {}

@app.post("/webhook")
async def webhook(request: Request):
    """
    Main Webhook with Full Payload Logging
    """
    form_data = await request.form()
    payload = dict(form_data)
    
    # 1. LOG THE PAYLOAD
    print("\n--- INCOMING TWILIO PAYLOAD ---")
    print(json.dumps(payload, indent=2, default=str))
    print("-------------------------------\n")

    sender = payload.get("From", "")
    text_body = payload.get("Body", "")
    media_url = payload.get("MediaUrl0") 
    media_type = payload.get("MediaContentType0", "")
    
    if not sender:
        return Response("Missing Sender", status_code=400)

    user_parts = []
    
    # Text
    if text_body:
        user_parts.append(text_body)
    
    # Media
    if media_url:
        print(f"DEBUG: Downloading Media: {media_url}")
        
        # --- THE DOWNLOAD FIX ---
        media_bytes, mime_type = await download_twilio_media(media_url)
        
        if media_bytes:
            # Gemini needs base64 string
            b64_data = base64.b64encode(media_bytes).decode('utf-8')
            
            if "audio" in mime_type:
                user_parts.append({
                    "inline_data": {
                        "mime_type": mime_type, 
                        "data": b64_data
                    }
                })
                user_parts.append("The user sent a voice note. Listen to it and respond.")
            
            elif "image" in mime_type:
                user_parts.append({
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": b64_data
                    }
                })
                user_parts.append("Analyze this dog image for breed/size/coat.")
        else:
            print("ERROR: Download returned empty bytes.")

    if not user_parts:
        return Response(str(MessagingResponse()), media_type="application/xml")

    # --- GEMINI CHAT ---
    if sender not in sessions:
        sessions[sender] = []
    history = sessions[sender]

    try:
        model = genai.GenerativeModel(
            model_name=MODEL_NAME,
            system_instruction="You are Bella, a friendly Pet Grooming receptionist. Use tools to check availability. SAVE dog details if you see a photo.",
            tools=tools
        )
        
        chat = model.start_chat(history=history, enable_automatic_function_calling=True)
        response = chat.send_message(user_parts)
        ai_reply = response.text

        # Update History
        sessions[sender].append({"role": "user", "parts": [text_body or "[Media]"]})
        sessions[sender].append({"role": "model", "parts": [ai_reply]})

    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        ai_reply = "Oops! I'm having trouble connecting to the brain. 🐕 Can you text me again?"

    twiml = MessagingResponse()
    twiml.message(ai_reply)
    return Response(content=str(twiml), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
