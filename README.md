# SSC (Sri Sai Cable & Broadband) - Ezybill Billing & Live Cash Ledger

**Project status: direct recharge is available for the operator's final click.** Selecting a plan loads a read-only portal quote automatically. The green **Activate selected plan** or **Extend current plan** button makes the final live request.

An automated cash-collection and subscriber renewal system for **`sms.sscbpl.com` (Ezybill SMS)**.

---

## 🌟 How It Works

1. **Direct Connection to Main SSC Portal (`sms.sscbpl.com`):**
   * Uses your operator credentials.
   * Solves the portal's image CAPTCHA automatically in the background using offline OCR.
   * Maintains your session so you don't have to repeatedly log in.
   * Displays the portal billed total and wallet debit separately from the cash price.

2. **100% Cash-Focused Workflow:**
   * **No payment gateways or SMS/WhatsApp APIs needed.**
   * Customer pays cash (or promises to pay later).
   * You search the customer (by Name, STB Number, or Mobile).
   * Choose **Renew previous plan**, **1 Month**, **6 Months**, or **1 Year**. Active customers also have **Extend current package** with a month count.
   * The dashboard checks the live portal quote automatically when you select a plan.
   * You click the green activation button after checking the customer and amounts.

3. **Local cash ledger:**
   * Successful portal submissions are recorded as pending cash collection in SQLite.
   * The dashboard shows Unpaid and Paid views in one collection frame, grouped by day.
   * Marking a recharge Paid moves it to the Paid view and updates the collection totals.

---

## 🚀 How to Run

### Step 1: Start the Dashboard
Install Python dependencies with `pip install -r requirements.txt`.
Double-click `start.bat` or run:
```bash
python app.py
```
Open **`http://localhost:5000`** in your browser (or open `http://<your-pc-ip>:5000` on your mobile phone connected to the same Wi-Fi!).

---

## ⚙️ Switching from Simulation Mode to Live SSC Portal

The system starts in **Simulation (Demo) Mode** so you can test and explore the interface safely.

To activate **Live Portal Mode**:
1. Click the **Settings (gear ⚙️)** icon in the top right.
2. Enter your **Ezybill Operator Username & Password**.
3. Uncheck **Simulation / Demo Mode**.
4. Click **"Save Settings"** and **"Connect Portal"**.

The three named packages are distinct portal products. Their cash prices are ₹280, ₹1680, and ₹3080. An active customer can extend the same package by multiple billing units; for example, a one-month package can be extended by two months using two units. The portal requires multiples of the current package's term and may reject a larger extension if the wallet balance is insufficient. For a package outside the three priced plans, enter the cash collection price before the final click. The portal rejects a different base package while the current package is active. Since the operator chose to preserve remaining prepaid service, a different package can be activated after that service expires. Deactivated customers can select a different package now. The read-only quote flows have been checked without submitting any recharge. The final live response has not been exercised during development because it would charge a real customer. If the app reports an unknown submission result, check the portal before retrying.

The dashboard listens on localhost by default. To expose it on a trusted LAN, set `web_host` to `0.0.0.0` and set a strong `SSC_APP_PASSWORD` environment variable before starting it. Keep `config.json` private because it stores portal credentials in plain text. Copy `config.example.json` to `config.json` for a fresh setup; never commit the filled-in file or `ssc_billing.db`.

## Hosting note

See [DEPLOY_ORACLE.md](DEPLOY_ORACLE.md) for the Oracle VM setup with HTTPS, Gunicorn, dashboard authentication, and persistent SQLite storage. Free hosts with an ephemeral filesystem can erase the payment ledger on restart or redeploy. Do not deploy this app there with live recharge enabled until persistent storage and access controls are in place.
