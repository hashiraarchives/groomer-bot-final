import express from "express";
import twilio from "twilio";

const app = express();

// Twilio sends application/x-www-form-urlencoded
app.use(express.urlencoded({ extended: false }));

// Health check
app.get("/", (req, res) => {
  res.send("Convervo WhatsApp bot is running");
});

// WhatsApp webhook
app.post("/webhooks/twilio/whatsapp", async (req, res) => {
  const from = req.body.From;   // whatsapp:+65...
  const to = req.body.To;       // whatsapp:+1464...
  const body = req.body.Body;   // message text

  console.log("Incoming WhatsApp message:", { from, to, body });

  const client = twilio(
    process.env.TWILIO_ACCOUNT_SID,
    process.env.TWILIO_AUTH_TOKEN
  );

  await client.messages.create({
    from: to,      // your Twilio WhatsApp number
    to: from,      // customer number
    body: "Hello 👋 This is Convervo. Your message was received!"
  });

  // Twilio expects a 200 response
  res.status(200).send("OK");
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
