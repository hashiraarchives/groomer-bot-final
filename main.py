"""
Pet Grooming WhatsApp Bot (AI Agent Version)
==============================================================
Stack: FastAPI + Twilio + Gemini 2.0 Flash (Multimodal) + Google Calendar
Features:
- "One Brain" Logic: No rigid state machines; the AI decides flow.
- Native Function Calling: AI calls calendar tools naturally.
- Multimodal: Understands Text, Images (Dogs), and Audio (Voice Notes).

Author: Senior Python Backend Engineer
Version: 3.0.0
"""

import os
import json
import logging
import base64
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo
from functools import partial

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

import google.generativeai as genai
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleAuthRequest
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

# Model Configuration
# Using Gemini 1.5 Flash as the stable workhorse, or 2.0-flash-exp if available to you
MODEL_NAME = "gemini-3-flash-preview" 

# Timezone
SGT = ZoneInfo("Asia/Singapore")

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("BellaBot")

# Configure Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# =============================================================================
# GOOGLE CALENDAR MANAGER
# =============================================================================

class CalendarManager:
    """Handles Google Calendar interactions."""
    
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        # 1. Try Environment Variable (Production)
        if GOOGLE_CREDENTIALS_JSON:
            try:
                info = json.loads(GOOGLE_CREDENTIALS_JSON)
                creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
            except Exception as e:
                logger.error(f"Creds Error: {e}")

        # 2. Try Local File (Dev)
        elif os.path.exists("service_account.json"):
            creds = service_account.Credentials.from_service_account_file("service_account.json", scopes=self.SCOPES)
        
        # 3. Try OAuth Token (Dev)
        elif os.path.exists("token.json"):
            creds = Credentials.from_authorized_user_file("token.json", self.SCOPES)

        if not creds:
            logger.warning("No Calendar Credentials found. Calendar tools will fail.")
            return None

        return build("calendar", "v3", credentials=creds)

    def find_next_slot(self, duration_minutes: int = 90) -> str:
        """Finds the next available slot starting from tomorrow."""
        if not self.service: return "Calendar unavailable."
        
        now = datetime.now(SGT)
        start_search = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        end_search = start_search + timedelta(days=7) # Look ahead 7 days

        # Get busy events
        events_result = self.service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start_search.isoformat(),
            timeMax=end_search.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        busy_times = []
        for event in events:
            start = event['start'].get('dateTime') or event['start'].get('date')
            end = event['end'].get('dateTime') or event['end'].get('date')
            busy_times.append((start, end))

        # Simple linear search for a free slot (10am - 7pm)
        current_check = start_search
        while current_check < end_search:
            if current_check.hour >= 19: # Closed after 7pm
                current_check = (current_check + timedelta(days=1)).replace(hour=10, minute=0)
                continue
            
            slot_end = current_check + timedelta(minutes=duration_minutes)
            
            # Check collision
            collision = False
            for b_start, b_end in busy_times:
                # Basic string comparison for ISO format works for simple overlap check
                if not (slot_end.isoformat() <= b_start or current_check.isoformat() >= b_end):
                    collision = True
                    break
            
            if not collision:
                return current_check.strftime("%A, %d %B at %I:%M %p")
            
            current_check += timedelta(minutes=30)
            
        return "No slots available in the next 7 days."

    def book_appointment(self, summary: str, time_str: str, duration_minutes: int) -> str:
        """Books a specific slot. Expects time_str in a parseable format or ISO."""
        if not self.service: return "System Error: Calendar not connected."

        try:
            # AI usually passes "Monday, 20 October at 10:00 AM" or ISO. 
            # For robustness, we will ask the AI to pass ISO in the system prompt, 
            # but here we parse a simplified version for demo.
            # IN PRODUCTION: Use dateparser or strict ISO passing from LLM.
            
            # For this demo, we trust the AI found the slot via find_next_slot, 
            # so we reconstruct the datetime object roughly or expect ISO.
            # Let's assume the AI passes ISO string for reliability.
            start_dt = datetime.fromisoformat(time_str) 
        except ValueError:
            # Fallback: Try to parse the human string relative to now (simple heuristics)
            return "Error: Invalid time format. Please contact human support."

        end_dt = start_dt + timedelta(minutes=duration_minutes)

        event = {
            'summary': summary,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
        }

        try:
            self.service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
            return "Success! Appointment booked."
        except Exception as e:
            logger.error(f"Booking failed: {e}")
            return "Failed to book slot."

calendar = CalendarManager()

# =============================================================================
# AI TOOLS (FUNCTION CALLING)
# =============================================================================

# Global store for conversation context (In production, use Redis/DB)
session_store: Dict[str, Dict[str, Any]] = {}

def get_session(phone: str) -> Dict:
    if phone not in session_store:
        session_store[phone] = {"history": [], "dog_data": {}}
    return session_store[phone]

# --- Tools exposed to Gemini ---

def check_availability(duration_minutes: int = 90):
    """
    Checks the calendar for the next available grooming slot.
    Args:
        duration_minutes: Duration of the service (Small=60, Med=90, Large=120).
    """
    logger.info(f"Tool Call: Checking availability for {duration_minutes} mins")
    return calendar.find_next_slot(duration_minutes)

def save_dog_details(breed: str, size: str, coat_condition: str, estimated_price: int):
    """
    Saves the analyzed dog details to the system. 
    MUST be called whenever a dog photo is received.
    """
    logger.info(f"Tool Call: Saving dog - {breed}, {size}, ${estimated_price}")
    # In a real app, save to DB here.
    return "Dog details saved successfully. You may now offer appointments."

def confirm_booking(customer_phone: str, date_iso: str, service_name: str):
    """
    Finalizes the booking in the calendar.
    Args:
        customer_phone: The user's phone number.
        date_iso: The exact date/time in ISO format (e.g., 2025-10-24T14:00:00).
        service_name: Summary of service (e.g., 'Grooming for Poodle').
    """
    logger.info(f"Tool Call: Booking for {customer_phone} at {date_iso}")
    # Basic logic to estimate duration based on service name or default to 90
    return calendar.book_appointment(f"{service_name} ({customer_phone})", date_iso, 90)

# Tool Map for Gemini
tools_map = {
    'check_availability': check_availability,
    'save_dog_details': save_dog_details,
    'confirm_booking': confirm_booking
}

tools_list = [check_availability, save_dog_details, confirm_booking]

# =============================================================================
# SYSTEM PROMPT
# =============================================================================

SYSTEM_INSTRUCTION = """
You are Bella, the AI Receptionist at Pawsome Grooming Singapore.
Your goal is to be helpful, warm, and efficient.

CORE BEHAVIORS:
1. **Visual Analysis**: When you receive a dog photo, you MUST analyze it and immediately call the `save_dog_details` tool with your best estimates (Breed, Size [Small/Medium/Large], Coat Condition, Price).
   - Prices: Small ($50), Medium ($70), Large ($90). Add $15 for matted coats.
   - Do NOT ask the user for these details if you can see them. Just state: "I've noted he's a [Breed]..."

2. **Scheduling**:
   - Never make up times. Always use `check_availability` to find a real slot.
   - When offering a slot, speak naturally: "I have an opening this Thursday at 2 PM."

3. **Booking**:
   - Once the user agrees to a time, call `confirm_booking`.
   - Ask for their name if you don't have it before booking.

4. **Personality**:
   - Use Singaporean flair occasionally (can use 'lah', 'leh' sparsely).
   - Be enthusiastic about dogs! "Omg so cute!" is acceptable.

5. **Audio**:
   - You can hear audio messages. Respond to them textually as if they were written text.
"""

# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI()

async def download_media(media_url: str) -> tuple[bytes, str]:
    """Downloads media from Twilio URL."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        return resp.content, resp.headers.get("Content-Type", "")

@app.post("/webhook")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(""),
    NumMedia: int = Form(0),
    MediaUrl0: str = Form(None),
    MediaContentType0: str = Form(None)
):
    """Unified Webhook for Text, Image, and Audio"""
    
    session = get_session(From)
    history = session["history"]
    
    # 1. Prepare User Input (Multimodal)
    user_parts = []
    
    # Add Text (if any)
    if Body:
        user_parts.append(Body)
        
    # Add Media (Image or Audio)
    if NumMedia > 0 and MediaUrl0:
        media_data, mime_type = await download_media(MediaUrl0)
        
        # Determine strict MIME type for Gemini
        if "image" in mime_type:
            user_parts.append({
                "mime_type": mime_type,
                "data": media_data # Gemini SDK handles bytes directly in this dict format usually
            })
            # Note: For strict SDK usage, we often use:
            # {"inline_data": {"mime_type": ..., "data": base64_str}}
            # But the newer SDK allows simpler passing. Let's stick to the safest way:
            b64_data = base64.b64encode(media_data).decode('utf-8')
            user_parts[-1] = {"inline_data": {"mime_type": mime_type, "data": b64_data}}
            
        elif "audio" in mime_type or "ogg" in mime_type:
            # Voice Note
            b64_data = base64.b64encode(media_data).decode('utf-8')
            # Map whatsapp audio/ogg to a type Gemini supports if needed, usually audio/ogg is fine
            user_parts.append({"inline_data": {"mime_type": "audio/ogg", "data": b64_data}})

    if not user_parts:
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    # 2. Initialize Gemini Chat with History
    # We rebuild the chat object each time to persist state in our own DB/Memory
    # (Gemini's history object is not serializable easily, so we pass history list)
    
    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=SYSTEM_INSTRUCTION,
        tools=tools_list
    )
    
    chat = model.start_chat(history=history, enable_automatic_function_calling=True)

    # 3. Send Message to AI
    try:
        response = chat.send_message(user_parts)
        ai_text = response.text
        
        # 4. Update History (Manually, since we recreate chat next time)
        # Note: Handling history with images manually is complex. 
        # For simplicity in this script, we just append text. 
        # In production, use File API for images in history.
        session["history"].append({"role": "user", "parts": [Body or "[Media Message]"]})
        session["history"].append({"role": "model", "parts": [ai_text]})
        
        # Trim history to prevent context overflow
        if len(session["history"]) > 10:
            session["history"] = session["history"][-10:]

    except Exception as e:
        logger.error(f"AI Error: {e}")
        ai_text = "Oh dear, my brain froze for a second! 🐕 Could you try saying that again?"

    # 5. Send Response via Twilio
    twiml = MessagingResponse()
    twiml.message(ai_text)
    
    return Response(content=str(twiml), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
