"""
Pet Grooming WhatsApp Bot - Twilio + Gemini + Google Calendar
==============================================================

A FastAPI-based WhatsApp bot for pet grooming appointments using:
- Twilio for WhatsApp messaging
- Google Gemini 2.0 Flash for dog image analysis
- Google Calendar API for appointment scheduling

Author: Senior Python Backend Engineer
Version: 2.0.0
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Literal
from enum import Enum
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
import google.generativeai as genai
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build

# =============================================================================
# CONFIGURATION
# =============================================================================
"""
REQUIRED ENVIRONMENT VARIABLES:
-------------------------------
1. GEMINI_API_KEY
   - Get from: https://aistudio.google.com/app/apikey

2. TWILIO_ACCOUNT_SID
   - Get from: https://console.twilio.com (Account Info section)

3. TWILIO_AUTH_TOKEN
   - Get from: https://console.twilio.com (Account Info section)

4. TWILIO_WHATSAPP_NUMBER
   - Your Twilio WhatsApp number (format: whatsapp:+14155238886)
   - Set up at: https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn

5. GOOGLE_CALENDAR_ID
   - Use 'primary' for default calendar or specific calendar ID

GOOGLE CALENDAR CREDENTIALS:
----------------------------
Option A: Service Account (Recommended for Production)
1. Go to Google Cloud Console > APIs & Services > Credentials
2. Create Service Account > Download JSON key
3. Rename to 'service_account.json' and place in project root
4. Share your calendar with the service account email

Option B: OAuth 2.0 (For Development)
1. Go to Google Cloud Console > APIs & Services > Credentials
2. Create OAuth 2.0 Client ID (Desktop app)
3. Download and rename to 'credentials.json'
4. First run will open browser for authorization
"""

# Environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
VALIDATE_TWILIO_SIGNATURE = os.getenv("VALIDATE_TWILIO_SIGNATURE", "true").lower() == "true"

# Google Calendar credentials as JSON string (for Railway/cloud deployment)
# Paste your entire service_account.json content as a single-line string
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

# Singapore timezone
SGT = ZoneInfo("Asia/Singapore")

# Business hours
BUSINESS_HOUR_START = 10  # 10 AM
BUSINESS_HOUR_END = 19    # 7 PM

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class DogAnalysis(BaseModel):
    """Result from Gemini vision analysis"""
    breed: str
    size: Literal["Small", "Medium", "Large"]
    estimated_duration_minutes: int
    coat_condition: str


class UserState(str, Enum):
    """Conversation states"""
    IDLE = "IDLE"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"


class UserSession(BaseModel):
    """User session data"""
    state: UserState = UserState.IDLE
    dog_analysis: Optional[DogAnalysis] = None
    proposed_slot: Optional[datetime] = None
    last_interaction: datetime = Field(default_factory=lambda: datetime.now(SGT))


class BookingDetails(BaseModel):
    """Appointment booking details"""
    phone: str
    breed: str
    size: str
    duration: int
    coat_condition: str
    start_time: datetime


# =============================================================================
# IN-MEMORY STATE STORAGE
# =============================================================================

user_sessions: dict[str, UserSession] = {}


def get_session(phone: str) -> UserSession:
    """Get or create user session"""
    if phone not in user_sessions:
        user_sessions[phone] = UserSession()
    return user_sessions[phone]


def update_session(phone: str, **kwargs) -> None:
    """Update user session"""
    session = get_session(phone)
    for key, value in kwargs.items():
        setattr(session, key, value)
    session.last_interaction = datetime.now(SGT)


def reset_session(phone: str) -> None:
    """Reset user session"""
    user_sessions[phone] = UserSession()


# =============================================================================
# TWILIO CLIENT
# =============================================================================

_twilio_client: Optional[TwilioClient] = None


def get_twilio_client() -> TwilioClient:
    """Get Twilio client singleton"""
    global _twilio_client
    if _twilio_client is None:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN required")
        _twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


def send_whatsapp_message(to: str, body: str) -> bool:
    """
    Send WhatsApp message via Twilio.

    Args:
        to: Recipient number (format: whatsapp:+1234567890)
        body: Message text

    Returns:
        True if sent successfully
    """
    try:
        client = get_twilio_client()
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to,
            body=body
        )
        logger.info(f"Message sent: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return False


# =============================================================================
# GOOGLE CALENDAR
# =============================================================================

SCOPES = ["https://www.googleapis.com/auth/calendar"]
_calendar_service = None


def get_calendar_service():
    """Get authenticated Google Calendar service"""
    global _calendar_service

    if _calendar_service is not None:
        return _calendar_service

    creds = None

    # Priority 1: Environment variable (for Railway/cloud deployment)
    if GOOGLE_CREDENTIALS_JSON:
        logger.info("Using GOOGLE_CREDENTIALS_JSON environment variable")
        try:
            creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=SCOPES
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse GOOGLE_CREDENTIALS_JSON: {e}")
            raise ValueError("Invalid JSON in GOOGLE_CREDENTIALS_JSON environment variable")

    # Priority 2: Service account file (local development)
    elif os.path.exists("service_account.json"):
        logger.info("Using service_account.json file")
        creds = service_account.Credentials.from_service_account_file(
            "service_account.json", scopes=SCOPES
        )

    # Priority 3: OAuth credentials file (local development)
    elif os.path.exists("credentials.json"):
        logger.info("Using OAuth credentials.json file")
        if os.path.exists("token.json"):
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(GoogleAuthRequest())
            else:
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
                creds = flow.run_local_server(port=0)

            with open("token.json", "w") as f:
                f.write(creds.to_json())
    else:
        raise ValueError(
            "No calendar credentials found. Set GOOGLE_CREDENTIALS_JSON env var "
            "or add 'service_account.json' file"
        )

    _calendar_service = build("calendar", "v3", credentials=creds)
    return _calendar_service


def get_next_available_slot(duration_minutes: int) -> Optional[datetime]:
    """
    Find next available slot in Google Calendar.

    Args:
        duration_minutes: Required appointment duration

    Returns:
        datetime of next available slot or None
    """
    try:
        service = get_calendar_service()
    except FileNotFoundError as e:
        logger.error(str(e))
        return None

    now = datetime.now(SGT)
    time_max = now + timedelta(days=14)

    # Fetch existing events
    try:
        events = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime"
        ).execute().get("items", [])
    except Exception as e:
        logger.error(f"Failed to fetch calendar: {e}")
        return None

    # Parse busy periods
    busy = []
    for event in events:
        start = event["start"].get("dateTime")
        end = event["end"].get("dateTime")
        if start and end:
            busy.append((
                datetime.fromisoformat(start.replace("Z", "+00:00")),
                datetime.fromisoformat(end.replace("Z", "+00:00"))
            ))

    busy.sort(key=lambda x: x[0])

    # Search for available slot
    duration = timedelta(minutes=duration_minutes + 15)  # 15 min buffer

    for day_offset in range(14):
        check_date = now.date() + timedelta(days=day_offset)

        day_start = datetime(
            check_date.year, check_date.month, check_date.day,
            BUSINESS_HOUR_START, 0, tzinfo=SGT
        )
        day_end = datetime(
            check_date.year, check_date.month, check_date.day,
            BUSINESS_HOUR_END, 0, tzinfo=SGT
        )

        # Skip past times for today
        if day_offset == 0 and now > day_start:
            slot_start = now.replace(second=0, microsecond=0)
            if slot_start.minute < 30:
                slot_start = slot_start.replace(minute=30)
            else:
                slot_start = (slot_start + timedelta(hours=1)).replace(minute=0)
        else:
            slot_start = day_start

        while slot_start + duration <= day_end:
            slot_end = slot_start + duration
            is_free = True

            for busy_start, busy_end in busy:
                busy_start_local = busy_start.astimezone(SGT)
                busy_end_local = busy_end.astimezone(SGT)

                if not (slot_end <= busy_start_local or slot_start >= busy_end_local):
                    is_free = False
                    slot_start = busy_end_local
                    break

            if is_free:
                return slot_start

            slot_start = slot_start + timedelta(minutes=30)

    return None


def book_slot(booking: BookingDetails) -> bool:
    """
    Create calendar event for booking.

    Args:
        booking: Appointment details

    Returns:
        True if booking created successfully
    """
    try:
        service = get_calendar_service()
    except FileNotFoundError:
        return False

    end_time = booking.start_time + timedelta(minutes=booking.duration)

    event = {
        "summary": f"Dog Grooming - {booking.breed}",
        "description": (
            f"Customer: {booking.phone}\n"
            f"Breed: {booking.breed}\n"
            f"Size: {booking.size}\n"
            f"Coat: {booking.coat_condition}\n"
            f"Duration: {booking.duration} min"
        ),
        "start": {
            "dateTime": booking.start_time.isoformat(),
            "timeZone": "Asia/Singapore"
        },
        "end": {
            "dateTime": end_time.isoformat(),
            "timeZone": "Asia/Singapore"
        },
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 60}]
        }
    }

    try:
        service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        logger.info(f"Booking created for {booking.phone}")
        return True
    except Exception as e:
        logger.error(f"Failed to create booking: {e}")
        return False


# =============================================================================
# GEMINI VISION
# =============================================================================

def configure_gemini():
    """Configure Gemini API"""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable not set")
    genai.configure(api_key=GEMINI_API_KEY)


async def analyze_dog_image(image_url: str) -> Optional[DogAnalysis]:
    """
    Analyze dog image using Gemini Vision.

    Args:
        image_url: URL of the dog image (from Twilio MediaUrl0)

    Returns:
        DogAnalysis result or None if failed
    """
    configure_gemini()

    # Use Gemini 2.0 Flash (latest available vision model)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = """Analyze this dog image and return ONLY a JSON object:
{
    "breed": "<detected breed>",
    "size": "<exactly one of: Small, Medium, Large>",
    "estimated_duration_minutes": <integer>,
    "coat_condition": "<normal/matted/tangled/well-maintained>"
}

Size guidelines:
- Small: under 10kg (Chihuahua, Pomeranian, etc.)
- Medium: 10-25kg (Beagle, Cocker Spaniel, etc.)
- Large: over 25kg (Golden Retriever, German Shepherd, etc.)

Duration guidelines:
- Small: 60 min base
- Medium: 90 min base
- Large: 120 min base
- Add 30 min if matted/tangled

Return ONLY the JSON, no markdown or extra text."""

    try:
        # Fetch image from URL and send to Gemini
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Twilio requires authentication to fetch media
            response = await client.get(
                image_url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            )
            response.raise_for_status()
            image_bytes = response.content

        # Create image part
        image_part = {"mime_type": "image/jpeg", "data": image_bytes}

        # Generate analysis
        result = model.generate_content([prompt, image_part])
        text = result.text.strip()

        # Clean markdown if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        data = json.loads(text)

        return DogAnalysis(
            breed=data["breed"],
            size=data["size"],
            estimated_duration_minutes=data["estimated_duration_minutes"],
            coat_condition=data["coat_condition"]
        )

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Gemini response: {e}")
        return None
    except Exception as e:
        logger.error(f"Gemini analysis failed: {e}")
        return None


# =============================================================================
# MESSAGE HANDLERS
# =============================================================================

async def handle_image(phone: str, media_url: str) -> str:
    """Handle incoming image message"""
    analysis = await analyze_dog_image(media_url)

    if not analysis:
        return (
            "Sorry, I couldn't see the dog clearly.\n\n"
            "Please send another photo with:\n"
            "- Good lighting\n"
            "- Full dog visible\n"
            "- Clear, non-blurry image"
        )

    slot = get_next_available_slot(analysis.estimated_duration_minutes)

    if not slot:
        return (
            f"I found a {analysis.size.lower()} {analysis.breed}!\n\n"
            f"Duration: {analysis.estimated_duration_minutes} min\n"
            f"Coat: {analysis.coat_condition}\n\n"
            "Unfortunately, we're fully booked for 2 weeks. "
            "Please try again later."
        )

    # Update session
    update_session(
        phone,
        state=UserState.AWAITING_CONFIRMATION,
        dog_analysis=analysis,
        proposed_slot=slot
    )

    # Calculate price
    prices = {"Small": 50, "Medium": 70, "Large": 90}
    price = prices[analysis.size]
    if "matted" in analysis.coat_condition.lower():
        price += 20

    date_str = slot.strftime("%A, %d %B")
    time_str = slot.strftime("%I:%M %p")

    return (
        f"Great! I analyzed your {analysis.breed}!\n\n"
        f"Size: {analysis.size}\n"
        f"Coat: {analysis.coat_condition}\n"
        f"Duration: {analysis.estimated_duration_minutes} min\n"
        f"Price: ${price}\n\n"
        f"Next slot: {date_str} at {time_str}\n\n"
        "Reply *Yes* to confirm or *No* to cancel."
    )


def handle_text(phone: str, text: str) -> str:
    """Handle incoming text message"""
    session = get_session(phone)
    text_lower = text.strip().lower()

    # Handle confirmation state
    if session.state == UserState.AWAITING_CONFIRMATION:
        if text_lower in ["yes", "y", "confirm", "ok", "sure"]:
            if session.dog_analysis and session.proposed_slot:
                booking = BookingDetails(
                    phone=phone,
                    breed=session.dog_analysis.breed,
                    size=session.dog_analysis.size,
                    duration=session.dog_analysis.estimated_duration_minutes,
                    coat_condition=session.dog_analysis.coat_condition,
                    start_time=session.proposed_slot
                )

                if book_slot(booking):
                    date_str = session.proposed_slot.strftime("%A, %d %B")
                    time_str = session.proposed_slot.strftime("%I:%M %p")
                    reset_session(phone)

                    return (
                        "Confirmed!\n\n"
                        f"Date: {date_str}\n"
                        f"Time: {time_str}\n"
                        f"Dog: {booking.breed}\n\n"
                        "Please arrive 10 min early.\n"
                        "See you soon!"
                    )
                else:
                    reset_session(phone)
                    return "Booking failed. Please try again or contact us directly."

            reset_session(phone)
            return "Session expired. Please send a photo to start again."

        elif text_lower in ["no", "n", "cancel", "nope"]:
            reset_session(phone)
            return "Cancelled. Send a photo anytime to book again."

        else:
            return "Reply *Yes* to confirm or *No* to cancel."

    # Default state - handle general messages
    greetings = ["hi", "hello", "hey"]
    if any(g in text_lower for g in greetings):
        return (
            "Welcome to Pet Grooming!\n\n"
            "Send a photo of your dog and I'll:\n"
            "- Identify the breed\n"
            "- Estimate grooming time\n"
            "- Find available slots\n\n"
            "Ready when you are!"
        )

    if "price" in text_lower or "cost" in text_lower:
        return (
            "Our Prices:\n\n"
            "Small dogs: from $50\n"
            "Medium dogs: from $70\n"
            "Large dogs: from $90\n\n"
            "Send a photo for exact quote!"
        )

    if "help" in text_lower:
        return (
            "How it works:\n\n"
            "1. Send a dog photo\n"
            "2. I'll analyze breed & size\n"
            "3. Get price & available time\n"
            "4. Reply Yes to book!\n\n"
            "Questions? Call +65 1234 5678"
        )

    return (
        "Send a photo of your dog to book a grooming appointment!\n\n"
        "I'll analyze the breed and find available slots."
    )


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(
    title="Pet Grooming WhatsApp Bot",
    description="Twilio + Gemini + Google Calendar",
    version="2.0.0"
)


@app.on_event("startup")
async def startup():
    """Validate configuration on startup"""
    logger.info("Starting Pet Grooming Bot...")

    warnings = []
    if not GEMINI_API_KEY:
        warnings.append("GEMINI_API_KEY not set")
    if not TWILIO_ACCOUNT_SID:
        warnings.append("TWILIO_ACCOUNT_SID not set")
    if not TWILIO_AUTH_TOKEN:
        warnings.append("TWILIO_AUTH_TOKEN not set")
    if not TWILIO_WHATSAPP_NUMBER:
        warnings.append("TWILIO_WHATSAPP_NUMBER not set")
    if not GOOGLE_CREDENTIALS_JSON and not os.path.exists("service_account.json") and not os.path.exists("credentials.json"):
        warnings.append("No Google Calendar credentials (set GOOGLE_CREDENTIALS_JSON or add credentials file)")

    for w in warnings:
        logger.warning(f"Warning: {w}")

    if not warnings:
        logger.info("All configurations OK")


@app.get("/")
async def root():
    """Health check"""
    return {"status": "healthy", "service": "Pet Grooming Bot"}


@app.get("/health")
async def health():
    """Detailed health check"""
    return {
        "status": "healthy",
        "gemini": bool(GEMINI_API_KEY),
        "twilio": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
        "calendar": bool(GOOGLE_CREDENTIALS_JSON) or os.path.exists("service_account.json") or os.path.exists("credentials.json"),
        "active_sessions": len(user_sessions)
    }


@app.post("/webhook")
async def webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(default=""),
    NumMedia: str = Form(default="0"),
    MediaUrl0: Optional[str] = Form(default=None),
    MediaContentType0: Optional[str] = Form(default=None),
):
    """
    Twilio WhatsApp webhook endpoint.

    Twilio sends form-encoded POST with fields:
    - From: Sender number (whatsapp:+1234567890)
    - Body: Message text
    - NumMedia: Number of media attachments
    - MediaUrl0: URL of first media attachment
    - MediaContentType0: MIME type of first media
    """
    # Validate Twilio signature (optional but recommended)
    if VALIDATE_TWILIO_SIGNATURE and TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        url = str(request.url)
        form_data = dict(await request.form())
        signature = request.headers.get("X-Twilio-Signature", "")

        if not validator.validate(url, form_data, signature):
            logger.warning("Invalid Twilio signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

    logger.info(f"Message from {From}: {Body[:50]}... Media: {NumMedia}")

    # Process message
    phone = From  # Format: whatsapp:+1234567890

    try:
        num_media = int(NumMedia)
    except ValueError:
        num_media = 0

    # Handle image if present
    if num_media > 0 and MediaUrl0:
        response_text = await handle_image(phone, MediaUrl0)
    else:
        response_text = handle_text(phone, Body)

    # Return TwiML response
    twiml = MessagingResponse()
    twiml.message(response_text)

    return Response(content=str(twiml), media_type="application/xml")


@app.get("/sessions")
async def list_sessions():
    """Debug: List active sessions"""
    return {
        phone: {
            "state": s.state.value,
            "has_analysis": s.dog_analysis is not None,
            "has_slot": s.proposed_slot is not None
        }
        for phone, s in user_sessions.items()
    }


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
