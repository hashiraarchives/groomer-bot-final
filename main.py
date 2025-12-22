import os
import json
import logging
import base64
import requests
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response, BackgroundTasks
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =============================================================================
# 1. CONFIGURATION
# =============================================================================

# Meta / WhatsApp Config
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN") # Permanent Token
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

# Google / Gemini Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Model Settings
# Note: Ensure you have access to gemini-2.0-flash-exp or similar if 3 isn't public yet via API
MODEL_NAME = "gemini-3-flash-preview" 

# Timezone
try:
    SGT = ZoneInfo("Asia/Singapore")
except:
    # Fallback if system doesn't have IANA database
    from datetime import timezone
    SGT = timezone(timedelta(hours=8))

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("BellaBot")

# Init Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# =============================================================================
# 2. MEDIA HANDLER (META VERSION)
# =============================================================================

async def download_meta_media(media_id: str) -> tuple[Optional[bytes], str]:
    """
    Downloads media from WhatsApp Cloud API.
    Step 1: Get URL from Media ID.
    Step 2: Download binary data with Auth headers.
    """
    if not media_id:
        return None, ""
    
    try:
        async with httpx.AsyncClient() as client:
            # 1. Get the Download URL
            url_endpoint = f"https://graph.facebook.com/v21.0/{media_id}"
            headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
            
            resp_url = await client.get(url_endpoint, headers=headers)
            if resp_url.status_code != 200:
                logger.error(f"Failed to get media URL: {resp_url.text}")
                return None, ""
                
            media_url = resp_url.json().get("url")
            mime_type = resp_url.json().get("mime_type")
            
            # 2. Download the Binary
            resp_data = await client.get(media_url, headers=headers)
            if resp_data.status_code != 200:
                logger.error(f"Failed to download bytes: {resp_data.status_code}")
                return None, ""
                
            logger.info(f"Downloaded {mime_type} ({len(resp_data.content)} bytes)")
            return resp_data.content, mime_type
            
    except Exception as e:
        logger.error(f"Media Download Error: {e}")
        return None, ""

# =============================================================================
# 3. GOOGLE CALENDAR TOOLS
# =============================================================================

class CalendarManager:
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        try:
            if GOOGLE_CREDENTIALS_JSON:
                info = json.loads(GOOGLE_CREDENTIALS_JSON)
                creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
            
            if creds:
                return build("calendar", "v3", credentials=creds)
        except Exception as e:
            logger.error(f"Auth Failed: {e}")
        return None

    def find_next_slot(self, duration_minutes: int = 90) -> str:
        if not self.service: return "Calendar Unavailable (Check Credentials)"
        
        now = datetime.now(SGT)
        # Search starting from tomorrow 10am
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

        busy_times = []
        for e in events_result.get('items', []):
            start = e['start'].get('dateTime') or e['start'].get('date')
            end = e['end'].get('dateTime') or e['end'].get('date')
            busy_times.append((start, end))
        
        curr = start_search
        while curr < end_search:
            # Skip nights (e.g., Closed after 7 PM)
            if curr.hour >= 19:
                curr = (curr + timedelta(days=1)).replace(hour=10, minute=0)
                continue
                
            is_busy = False
            slot_end = curr + timedelta(minutes=duration_minutes)
            
            # Simple Overlap Check
            # Note: For production, parse ISO strings properly. 
            # Here assuming standard Google format for simplicity.
            slot_start_iso = curr.isoformat()
            slot_end_iso = slot_end.isoformat()
            
            for b_start, b_end in busy_times:
                # Basic string comparison often works for ISO8601 same-timezone
                # Ideally, use datetime objects for strict comparison
                if not (slot_end_iso <= b_start or slot_start_iso >= b_end):
                    is_busy = True
                    break

            if not is_busy:
                return f"Available: {curr.strftime('%A, %d %B at %I:%M %p')}. (ISO: {slot_start_iso})"
            
            curr += timedelta(minutes=30)

        return "No slots available next 5 days."

    def book_appointment(self, customer_info: str, time_str: str) -> str:
        if not self.service: return "Calendar Error."
        try:
            # Parse ISO (Handling potential Z or offset issues loosely)
            start_dt = datetime.fromisoformat(time_str)
        except:
            return "Error: Please provide date in ISO format (YYYY-MM-DDTHH:MM:SS)"
            
        event = {
            'summary': f"Grooming: {customer_info}",
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Singapore'},
            'end': {'dateTime': (start_dt + timedelta(minutes=90)).isoformat(), 'timeZone': 'Asia/Singapore'},
        }
        try:
            self.service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
            return "Booking Confirmed! ✅"
        except Exception as e:
            logger.error(f"Booking Error: {e}")
            return "Failed to book slot system error."

calendar = CalendarManager()

# --- GEMINI TOOLS DEFINITION ---
def check_availability(duration: int = 90):
    """Checks calendar for the next available slot."""
    return calendar.find_next_slot(duration)

def book_slot(customer_info: str, iso_date: str):
    """Books a slot. customer_info: Name/Breed. iso_date: Exact ISO format string from check tool."""
    return calendar.book_appointment(customer_info, iso_date)

tools = [check_availability, book_slot]

# =============================================================================
# 4. FASTAPI APP & META WEBHOOK
# =============================================================================

app = FastAPI()

# Simple In-Memory History
sessions: Dict[str, List[Dict]] = {}

def send_whatsapp_text(to_number: str, text: str):
    """Helper to send text back to Meta."""
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "text": {"body": text}
    }
    try:
        r = requests.post(url, json=payload, headers=headers)
        if r.status_code not in [200, 201]:
            logger.error(f"Meta Send Error: {r.text}")
    except Exception as e:
        logger.error(f"Meta Network Error: {e}")

async def process_message(data: dict):
    """
    Background Task: 
    1. Parse Meta JSON
    2. Download Media (if any)
    3. Query Gemini
    4. Reply
    """
    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        
        if "messages" not in value:
            return # Status update or other event

        msg = value["messages"][0]
        sender = msg["from"]
        msg_type = msg["type"]
        
        logger.info(f"Processing Msg from {sender} | Type: {msg_type}")

        # 1. Prepare Inputs for Gemini
        user_parts = []
        
        # Handle Text
        if msg_type == "text":
            user_parts.append(msg["text"]["body"])
            
        # Handle Image/Audio
        elif msg_type in ["image", "audio", "voice"]:
            # Meta provides an ID, we must fetch the URL then the Blob
            media_id = msg[msg_type].get("id")
            media_bytes, mime_type = await download_meta_media(media_id)
            
            if media_bytes:
                b64_data = base64.b64encode(media_bytes).decode('utf-8')
                user_parts.append({
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": b64_data
                    }
                })
                if msg_type == "audio" or msg_type == "voice":
                    user_parts.append("This is a user voice note. Listen and reply kindly.")
                else:
                    user_parts.append("Analyze this image (pet details?).")

        if not user_parts:
            logger.warning("No content found to send to AI.")
            return

        # 2. Manage History
        if sender not in sessions:
            sessions[sender] = []
        
        # 3. Call Gemini
        model = genai.GenerativeModel(
            model_name=MODEL_NAME,
            system_instruction="""You are Bella, the receptionist at ABC Grooming. 
            - You are friendly, capable, and professional.
            - You can see images (identify breed/coat) and hear voice notes.
            - Use tools to CHECK availability first, then BOOK.
            - If user sends a photo, comment on how cute the dog is.
            - Keep replies concise for WhatsApp.""",
            tools=tools
        )
        
        # Re-construct chat object with history
        chat = model.start_chat(history=sessions[sender], enable_automatic_function_calling=True)
        
        response = chat.send_message(user_parts)
        ai_reply = response.text

        # 4. Save to History & Reply
        # Note: We store text representation of media for history simplicity
        sessions[sender].append({"role": "user", "parts": ["User sent message/media."]})
        sessions[sender].append({"role": "model", "parts": [ai_reply]})
        
        send_whatsapp_text(sender, ai_reply)

    except Exception as e:
        logger.error(f"Logic Error: {e}")
        # Optional fallback reply
        # send_whatsapp_text(sender, "Oops, my brain froze. One moment!")

@app.get("/meta-webhook")
async def verify_webhook(request: Request):
    """Meta Verification Handshake"""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, status_code=200)
    return Response(content="Verification failed", status_code=403)

@app.post("/meta-webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    """
    Main Entry Point
    """
    try:
        data = await request.json()
        # Spawn background task to prevent timeout
        background_tasks.add_task(process_message, data)
    except Exception as e:
        logger.error(f"JSON Parse Error: {e}")
        
    return Response(content="OK", status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
