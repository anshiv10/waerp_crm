# Meta (WhatsApp Cloud API) setup — per client

Do this once for every client business. Screens move around over time, but the objects stay the same.

1. **Business portfolio** — the client needs a Meta Business portfolio (business.facebook.com).
   Complete **Business Verification**. It is needed for the display name, higher messaging limits and going live.
2. **App** — go to developers.facebook.com → *Create App* → type **Business** → add the **WhatsApp** product.
   Link the app to the client's portfolio.
3. **Phone number** — WhatsApp → *API Setup* → *Add phone number*. Verify it by SMS or voice.
   Note two IDs from this screen:
   - **Phone number ID** → WA Account › Phone Number ID
   - **WhatsApp Business Account ID** → WA Account › WhatsApp Business Account ID

   A number already in use on the WhatsApp app must be migrated, or connected through Meta's
   *coexistence* onboarding where it's available.
4. **Permanent token** — Business Settings → *Users › System users* → add an **Admin** system user.
   1. *Assign assets*: the app and the WhatsApp account, with **full control**.
   2. *Generate token*, expiry **Never**.
   3. Grant the scopes `whatsapp_business_messaging` and `whatsapp_business_management`.
   4. Paste the token → WA Account › Permanent Access Token.

   The temporary token on the API Setup page dies after 24 hours, so don't use it.
5. **App Secret** — App settings → *Basic* → App Secret → WA Account › App Secret.
   This secret is used to verify every webhook.
6. **Webhook** — WhatsApp → *Configuration* → *Edit*:
   1. Callback URL: the **Webhook Callback URL** shown on the WA Account form, i.e.
      `https://<site>/api/method/waerp_crm.api.webhook.receive`.
   2. Verify token: the same random string you saved in WA Account › Webhook Verify Token.
   3. Click *Verify and save*.
   4. Under *Webhook fields*, subscribe to: `messages`, `message_template_status_update`,
      `message_template_quality_update`, `template_category_update`, `phone_number_quality_update`.
7. In ERPNext, open the WA Account and click **Meta → Subscribe Webhooks**. This subscribes the app to the WABA.
   Then click **Test Connection** and **Sync Templates**.
8. **Go live** — set the app to *Live* mode. This needs a privacy-policy URL.
   - While the app is in development, only the test recipients you add can receive messages (error 131030).
   - Add a payment method on the WhatsApp account. Business-initiated templates are billed by Meta.

## Rules that the app enforces for you

- Free-form messages are only allowed within **24 h of the customer's last message**. After that, only
  **approved templates** can be sent. The inbox shows this, and the API refuses with a clear error.
- Marketing broadcasts skip contacts who replied **STOP** (the keywords are configurable).
- Template variables can't be empty, so blanks are sent as "-".
