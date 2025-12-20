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
    friendly_comment: str = ""


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
# GEMINI AI - THE BRAIN OF THE BOT
# =============================================================================

# System prompt that defines the AI receptionist personality
RECEPTIONIST_SYSTEM_PROMPT = """You are Bella, a warm and friendly receptionist at "Pawsome Grooming" - a premium pet grooming salon in Singapore. You genuinely love dogs and it shows in how you talk about them.

YOUR PERSONALITY:
- Warm, friendly, and enthusiastic about dogs
- Professional but not stiff - you chat like a real person
- You notice specific details about each dog and comment on them
- You use emojis naturally but not excessively
- You're helpful and patient, even with confused customers

YOUR JOB:
1. When customers send dog photos: Analyze the dog, identify breed/size, assess coat condition, and offer a grooming appointment
2. When customers chat: Answer questions about services, pricing, or just have a friendly conversation
3. Guide customers through the booking process naturally

PRICING (Singapore Dollars):
- Small dogs (under 10kg): $50-60
- Medium dogs (10-25kg): $70-80
- Large dogs (over 25kg): $90-110
- Add $15-25 for matted/tangled coats
- Add $10 for special treatments (de-shedding, teeth cleaning, nail art)

GROOMING DURATION:
- Small: ~60 minutes
- Medium: ~90 minutes
- Large: ~120 minutes
- Add 30 mins for matted coats

BUSINESS HOURS: 10 AM - 7 PM daily (Singapore Time)

IMPORTANT RULES:
- Always be genuine and specific - never give generic responses
- When you see a dog photo, describe what YOU actually see (color, expression, features)
- If you can't clearly see the dog, ask for another photo nicely
- Keep responses concise - this is WhatsApp, not email
- Use *bold* for important info like dates and times
- When confirming bookings, be excited for the customer!"""


def configure_gemini():
    """Configure Gemini API"""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable not set")
    genai.configure(api_key=GEMINI_API_KEY)


def get_gemini_model():
    """Get configured Gemini model with system prompt"""
    configure_gemini()
    return genai.GenerativeModel(
        model_name="gemini-2.5-flash-preview-05-20",
        system_instruction=RECEPTIONIST_SYSTEM_PROMPT
    )


async def download_image(image_url: str) -> tuple[bytes, str]:
    """Download image from Twilio URL"""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            image_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "image/jpeg")
        if ";" in content_type:
            content_type = content_type.split(";")[0].strip()

        return response.content, content_type


async def analyze_dog_with_gemini(image_url: str, user_message: str = "") -> tuple[Optional[DogAnalysis], str]:
    """
    Analyze dog image and generate a natural response using Gemini.

    Returns:
        Tuple of (DogAnalysis or None, response_message)
    """
    import base64

    try:
        # Download the image
        image_bytes, content_type = await download_image(image_url)
        logger.info(f"Downloaded image: {len(image_bytes)} bytes, type: {content_type}")

        model = get_gemini_model()

        # Create image part
        image_part = {
            "inline_data": {
                "mime_type": content_type,
                "data": base64.b64encode(image_bytes).decode("utf-8")
            }
        }

        # Prompt for structured analysis + natural response
        analysis_prompt = f"""A customer just sent this photo of their dog{f' with message: "{user_message}"' if user_message else ''}.

First, analyze the dog and extract this information (for our booking system):
- breed: Your best guess at the breed or mix
- size: exactly "Small", "Medium", or "Large"
- estimated_duration_minutes: integer (60/90/120 + 30 if matted)
- coat_condition: describe what you see
- price: estimated price in SGD

Then, write a friendly response to the customer. In your response:
1. Comment on something SPECIFIC you notice about their dog (color, expression, cute features)
2. Share the grooming details naturally
3. Offer them an available appointment slot

RESPOND IN THIS EXACT FORMAT:
===ANALYSIS===
breed: <breed>
size: <Small/Medium/Large>
duration: <minutes>
coat: <condition>
price: <amount>
===RESPONSE===
<your friendly message to the customer>"""

        result = model.generate_content([analysis_prompt, image_part])
        response_text = result.text.strip()

        logger.info(f"Gemini full response:\n{response_text}")

        # Parse the structured response
        analysis = None
        message = response_text

        if "===ANALYSIS===" in response_text and "===RESPONSE===" in response_text:
            parts = response_text.split("===RESPONSE===")
            analysis_part = parts[0].replace("===ANALYSIS===", "").strip()
            message = parts[1].strip() if len(parts) > 1 else response_text

            # Parse analysis fields
            try:
                lines = analysis_part.split("\n")
                data = {}
                for line in lines:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        data[key.strip().lower()] = value.strip()

                # Map size string
                size = data.get("size", "Medium")
                if size not in ["Small", "Medium", "Large"]:
                    size = "Medium"

                # Parse duration
                duration = 90
                try:
                    duration = int(''.join(filter(str.isdigit, data.get("duration", "90"))))
                except:
                    pass

                analysis = DogAnalysis(
                    breed=data.get("breed", "Mixed breed"),
                    size=size,
                    estimated_duration_minutes=duration,
                    coat_condition=data.get("coat", "normal"),
                    friendly_comment=""
                )

            except Exception as e:
                logger.error(f"Failed to parse analysis: {e}")

        return analysis, message

    except Exception as e:
        logger.error(f"Gemini analysis failed: {e}")
        return None, (
            "Hmm, I'm having a bit of trouble seeing that photo clearly! 📸\n\n"
            "Could you send another one? A nice bright shot where I can see your pup's full face and body works best!"
        )


async def chat_with_gemini(user_message: str, context: dict) -> str:
    """
    Generate a conversational response using Gemini.

    Args:
        user_message: The user's text message
        context: Session context (state, dog_analysis, proposed_slot, etc.)
    """
    try:
        model = get_gemini_model()

        # Build context for Gemini
        context_info = []

        if context.get("state") == "AWAITING_CONFIRMATION":
            analysis = context.get("dog_analysis")
            slot = context.get("proposed_slot")
            if analysis and slot:
                context_info.append(f"CURRENT CONTEXT: Customer is deciding on a booking.")
                context_info.append(f"Dog: {analysis.breed} ({analysis.size})")
                context_info.append(f"Duration: {analysis.estimated_duration_minutes} mins")
                context_info.append(f"Proposed slot: {slot.strftime('%A, %d %B at %I:%M %p')}")
                context_info.append("If they confirm (yes/ok/sure/etc), celebrate and confirm the booking!")
                context_info.append("If they decline (no/cancel/etc), be understanding and invite them back.")

        context_str = "\n".join(context_info) if context_info else "Customer is starting a new conversation."

        prompt = f"""{context_str}

Customer message: "{user_message}"

Respond naturally as Bella the receptionist. Keep it concise for WhatsApp.
If they're asking about services/pricing, be helpful.
If they want to book, ask them to send a dog photo.
If they're confirming/declining a booking, respond appropriately."""

        result = model.generate_content(prompt)
        return result.text.strip()

    except Exception as e:
        logger.error(f"Gemini chat failed: {e}")
        return (
            "I'd love to help you book a grooming session! 🐕\n\n"
            "Just send me a photo of your dog and I'll check what slots are available."
        )


# =============================================================================
# MESSAGE HANDLERS
# =============================================================================

async def handle_image(phone: str, media_url: str, caption: str = "") -> str:
    """Handle incoming image message using Gemini AI"""

    # Get analysis and AI-generated response
    analysis, ai_response = await analyze_dog_with_gemini(media_url, caption)

    if not analysis:
        # Gemini couldn't analyze - return the error message it generated
        return ai_response

    # Find next available slot
    slot = get_next_available_slot(analysis.estimated_duration_minutes)

    if not slot:
        # No slots available - let Gemini know and regenerate response
        return (
            f"{ai_response}\n\n"
            "Unfortunately, we're fully booked for the next two weeks! 😔 "
            "Feel free to check back soon or give us a call to join the waitlist."
        )

    # Update session with booking info
    update_session(
        phone,
        state=UserState.AWAITING_CONFIRMATION,
        dog_analysis=analysis,
        proposed_slot=slot
    )

    # Append slot info to Gemini's response if not already mentioned
    date_str = slot.strftime("%A, %d %B")
    time_str = slot.strftime("%I:%M %p")

    # Check if response already mentions a specific time
    if "slot" not in ai_response.lower() and "available" not in ai_response.lower():
        ai_response += f"\n\nI have an opening on *{date_str}* at *{time_str}* - would that work for you? Just reply *Yes* to book or *No* to pass!"
    else:
        # Replace placeholder slot mention with actual slot
        ai_response += f"\n\nReply *Yes* to confirm *{date_str}* at *{time_str}*, or *No* if that doesn't work!"

    return ai_response


async def handle_text(phone: str, text: str) -> str:
    """Handle incoming text message using Gemini AI"""
    session = get_session(phone)
    text_lower = text.strip().lower()

    # Check if user is confirming/declining a booking
    if session.state == UserState.AWAITING_CONFIRMATION:
        # Positive confirmations
        if any(word in text_lower for word in ["yes", "yep", "yeah", "ok", "okay", "sure", "confirm", "book", "sounds good", "let's do it", "perfect"]):
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
                    breed = booking.breed
                    reset_session(phone)

                    # Let Gemini generate the confirmation message
                    context = {
                        "action": "booking_confirmed",
                        "date": date_str,
                        "time": time_str,
                        "breed": breed
                    }
                    return await chat_with_gemini(
                        f"Customer confirmed booking for their {breed} on {date_str} at {time_str}. Celebrate with them!",
                        context
                    )
                else:
                    reset_session(phone)
                    return await chat_with_gemini(
                        "The booking system had an error. Apologize and ask them to try again.",
                        {}
                    )

            reset_session(phone)
            return await chat_with_gemini(
                "Customer's session expired. Ask them to send a new dog photo.",
                {}
            )

        # Negative responses
        elif any(word in text_lower for word in ["no", "nope", "nah", "cancel", "not now", "maybe later", "don't", "cant", "can't"]):
            reset_session(phone)
            return await chat_with_gemini(
                "Customer declined the booking. Be understanding and invite them to book another time.",
                {}
            )

    # Build context for Gemini
    context = {
        "state": session.state.value if session.state else "IDLE",
        "dog_analysis": session.dog_analysis,
        "proposed_slot": session.proposed_slot
    }

    # Let Gemini handle all other conversations
    return await chat_with_gemini(text, context)


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

    # Handle image if present (Body may contain caption)
    if num_media > 0 and MediaUrl0:
        response_text = await handle_image(phone, MediaUrl0, caption=Body)
    else:
        response_text = await handle_text(phone, Body)

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
