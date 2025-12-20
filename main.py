"""
Pet Grooming WhatsApp Bot (Fix Checklist Edition)
==============================================================
Stack: FastAPI + Twilio + Gemini 3 Flash + Google Calendar
Updates:
- Implemented "Whole Payload" Debugging for Railway logs
- Added Secure Media Download (Fixes "Gemini can't read URL" issue)
- Fixed "MediaUrl0 Trap" by prioritizing media over text body
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
# MEDIA HANDLER (THE FIX FOR VOICE/IMAGES)
# =============================================================================

async def download_twilio_media(media_url: str) -> tuple[bytes, str]:
    """
    CRITICAL FIX: Downloads media securely from Twilio.
    Gemini cannot access Twilio URLs directly because they are password protected.
    We must download the bytes here using our credentials.
    """
    if not media_url:
        return None, None

    async with httpx.AsyncClient() as client:
        # We use Basic Auth with Account SID + Auth Token
        response = await client.get(
            media_url, 
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=30.0
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to download media: {response.status_code}")
            return None, None
            
        content_type = response.headers.get("Content-Type", "")
        return response.content, content_type

# =============================================================================
# GOOGLE CALENDAR & TOOLS
# =============================================================================

class CalendarManager:
    # (Same robust calendar logic as V3)
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
        if not self.service: return "Calendar unavailable (Check Credentials)."
        now = datetime.now(SGT)
        # Search from tomorrow 10 AM
        start_search = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        end_search = start_search + timedelta(days=5)

        events_result = self.service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start_search.isoformat(),
            timeMax=end_search.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        busy_times = [(e['start'].get('dateTime'), e['end'].get('dateTime')) for e in events_result.get('items', [])]

        # Simple linear search
        curr = start_search
        while curr < end_search:
            if curr.hour >= 19:
                curr = (curr + timedelta(days=1)).replace(hour=10, minute=0)
                continue
            
            # Simple check (expand logic for production)
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
            # Note: real app should accept ISO from AI. 
            # This is a simplified demo parser or expects ISO.
            start_dt = datetime.fromisoformat(time_str) 
        except:
            return "Error: Please provide date in ISO format (YYYY-MM-DDTHH:MM:SS)"
            
        event = {
            'summary': summary,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
            'end': {'dateTime': (start_dt + timedelta(minutes=90)).isoformat(), 'timeZone': 'Asia/Singapore'},
        }
        self.service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return "Booked!"

calendar = CalendarManager()

# --- GEMINI TOOLS ---
def check_availability(duration_minutes: int = 90):
    """Check for open slots."""
    return calendar.find_next_slot(duration_minutes)

def save_dog_details(breed: str, size: str, coat: str, price: int):
    """Save dog info to database."""
    # (Mock DB save)
    return "Details Saved."

def confirm_booking(customer_phone: str, date_iso: str, service: str):
    """Book the slot."""
    return calendar.book_appointment(f"{service} ({customer_phone})", date_iso)

tools = [check_availability, save_dog_details, confirm_booking]

# =============================================================================
# FASTAPI WEBHOOK
# =============================================================================

app = FastAPI()

# Session store (In-memory)
sessions: Dict[str, List[Dict]] = {}

@app.post("/webhook")
async def webhook(request: Request):
    """
    Main Webhook: Handles Text, Images, and Audio.
    Uses 'The Debugger' method to print full payload.
    """
    
    # --- CHECKLIST ITEM #2: DEBUGGING ---
    # We parse the form data manually to print the WHOLE payload
    form_data = await request.form()
    payload = dict(form_data)
    
    # PRINT RAW PAYLOAD TO RAILWAY LOGS
    print("\n--- INCOMING TWILIO PAYLOAD ---")
    print(json.dumps(payload, indent=2, default=str))
    print("-------------------------------\n")

    # --- CHECKLIST ITEM #1: DATA STRUCTURE ---
    # Extract fields safely using the correct keys
    sender = payload.get("From", "")
    text_body = payload.get("Body", "")
    media_url = payload.get("MediaUrl0") # The trap! Check this explicitly.
    media_type = payload.get("MediaContentType0", "")
    
    if not sender:
        return Response("Missing Sender", status_code=400)

    # Prepare Gemini Input
    user_parts = []
    
    # 1. Handle Text
    if text_body:
        user_parts.append(text_body)
    
    # 2. Handle Media (Images OR Voice Notes)
    if media_url:
        print(f"DEBUG: Found Media! Type: {media_type}, URL: {media_url}")
        
        # --- CHECKLIST ITEM #3: SECURE DOWNLOAD ---
        # We download bytes because Gemini cannot read the private Twilio URL
        media_bytes, mime_type = await download_twilio_media(media_url)
        
        if media_bytes:
            # Voice Note Handling
            if "audio" in mime_type:
                # Gemini handles 'audio/ogg', 'audio/mp3' etc.
                user_parts.append({
                    "inline_data": {
                        "mime_type": mime_type, # e.g. "audio/ogg"
                        "data": base64.b64encode(media_bytes).decode('utf-8')
                    }
                })
                user_parts.append("The user sent a voice note. Listen to it and respond.")
            
            # Image Handling
            elif "image" in mime_type:
                user_parts.append({
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(media_bytes).decode('utf-8')
                    }
                })
                user_parts.append("Analyze this dog image for breed/size/coat.")

    # Fallback if empty (e.g., weird ping)
    if not user_parts:
        return Response(str(MessagingResponse()), media_type="application/xml")

    # --- GEMINI INTERACTION ---
    
    # Get History
    if sender not in sessions:
        sessions[sender] = []
    history = sessions[sender]

    try:
        model = genai.GenerativeModel(
            model_name=MODEL_NAME,
            system_instruction="You are Bella, a friendly Pet Grooming receptionist in Singapore. Use tools to check availability and book. Save dog details when you see images.",
            tools=tools
        )
        
        chat = model.start_chat(history=history, enable_automatic_function_calling=True)
        
        # Send to AI
        response = chat.send_message(user_parts)
        ai_reply = response.text

        # Update History (Simple Text Append for demo)
        sessions[sender].append({"role": "user", "parts": [text_body or "[Media]"]})
        sessions[sender].append({"role": "model", "parts": [ai_reply]})

    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        ai_reply = "Oops! My brain froze for a second. 🐕 Can you try again?"

    # Send WhatsApp Reply
    twiml = MessagingResponse()
    twiml.message(ai_reply)
    return Response(content=str(twiml), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
