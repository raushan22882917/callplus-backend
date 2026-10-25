# Turn on 1-click WhatsApp setup ("Connect WhatsApp with Facebook")

Do this **once**, as the platform owner. After it, every client connects WhatsApp
by clicking one button, logging in to Facebook and confirming an SMS code. The
manual "paste your token" form disappears for them automatically.

Your addresses (used below):

| What | Value |
|---|---|
| Website (frontend) | `https://web-production-2b784.up.railway.app` |
| API | `https://api.callplus.autobot360.com` |
| WhatsApp webhook URL | `https://api.callplus.autobot360.com/api/v1/webhooks/whatsapp/meta` |

## 1. Meta Business account (≈10 min, approval takes 1–5 days)

1. Go to <https://business.facebook.com> and create (or open) your **Business portfolio** for CallPulse.
2. **Settings → Security Center → Start verification.** Upload a business document
   (GST certificate, Udyam, or incorporation certificate) and verify the phone/email/domain.
   Meta must approve this before clients outside your team can connect.

## 2. Meta app (≈15 min)

1. <https://developers.facebook.com/apps> → **Create app** → use case
   **"Connect with customers through WhatsApp"** (type *Business*) → pick your Business portfolio.
2. **App settings → Basic:** note the **App ID** and click *Show* on the **App secret**.
   Add your privacy-policy URL and app icon (required to go Live).

## 3. Webhook, so messages reach CallPulse

1. In the app: **WhatsApp → Configuration → Webhook → Edit**.
2. Callback URL: the *WhatsApp webhook URL* from the table above.
3. Verify token: the value of `META_WHATSAPP_VERIFY_TOKEN` on the Railway **api** service
   (Railway → api → Variables). Click **Verify and save**.
4. Under *Webhook fields*, **Subscribe** to `messages` and `message_template_status_update`
   (the second one tells CallPulse when Meta approves or rejects a message template).

## 4. The signup popup (Embedded Signup configuration)

1. Add the product **Facebook Login for Business**.
2. **Facebook Login for Business → Settings:**
   - *Login with the JavaScript SDK*: **Yes**
   - *Allowed Domains for the JavaScript SDK*: `https://web-production-2b784.up.railway.app`
   - *Valid OAuth Redirect URIs*: `https://web-production-2b784.up.railway.app/`
3. **Facebook Login for Business → Configurations → Create configuration:**
   - Name: `CallPulse WhatsApp signup`
   - Login variation: **WhatsApp Embedded Signup**
   - Assets: **WhatsApp accounts**; permissions: `whatsapp_business_management`, `whatsapp_business_messaging`
   - Save, then copy the **Configuration ID**.

## 5. Become a Tech Provider (App Review)

1. **App Review → Permissions and features:** request **Advanced access** for
   `whatsapp_business_management` and `whatsapp_business_messaging`. Meta asks for a short
   screen recording: record clicking *Connect WhatsApp with Facebook* on the WhatsApp page
   and sending/receiving a message.
2. When approved (and the business is verified), switch the app to **Live** (top of the dashboard).

Until approval, the button already works for anyone with a role on the app
(App roles → add yourself / testers), so you can try it end to end today.

## 6. Tell CallPulse (Railway → `call-center-backend` → **api** → Variables)

| Variable | Value |
|---|---|
| `META_APP_ID` | App ID from step 2 |
| `META_APP_SECRET` | App secret from step 2 (paste it yourself; keep it private) |
| `META_WHATSAPP_CONFIG_ID` | Configuration ID from step 4 |

Railway redeploys on save. Then open the **WhatsApp** page in CallPulse: the
*Connect WhatsApp with Facebook* button is enabled and the manual form is gone.

## What your client sees

1. WhatsApp page → **Connect WhatsApp with Facebook**.
2. Log in to Facebook, pick or create their business.
3. Enter the phone number customers will message, type the SMS/call code.
4. Done. CallPulse stores everything (token encrypted) and shows a 6-digit
   two-step-verification PIN once, to save.

Tips for clients: the number must not be on the regular WhatsApp app. A number
already on the WhatsApp Business app can be linked (Meta offers this in the popup).
