import os
import sys
import time
import json
import logging
import requests
from ssc_engine import engine
from database import get_today_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TelegramBot] %(message)s")
logger = logging.getLogger("TelegramBot")

class SSCTelegramBot:
    def __init__(self):
        self.load_token()
        self.last_update_id = 0

    def load_token(self):
        self.token = engine.config.get("telegram_bot_token", "").strip()
        self.allowed_chats = engine.config.get("telegram_allowed_chat_ids", [])
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, chat_id, text):
        if not self.token:
            return
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Error sending telegram message: {e}")

    def handle_command(self, chat_id, text):
        parts = text.strip().split()
        if not parts:
            return

        cmd = parts[0].lower()

        # /start or /help
        if cmd in ["/start", "/help"]:
            help_msg = (
                "⚡ *SSC Ezybill Billing Bot*\n\n"
                "Available Commands:\n"
                "• `/balance` - Check operator wallet balance\n"
                "• `/check <STB_NO>` - Lookup subscriber details & plan\n"
                "• `/renew <STB_NO> [amount]` - 1-Click cash renewal\n"
                "• `/today` - Today's cash collected and count\n"
                "• `/status` - Portal connection status"
            )
            self.send_message(chat_id, help_msg)

        # /balance
        elif cmd == "/balance":
            bal = engine.fetch_wallet_balance()
            self.send_message(chat_id, f"💳 *Operator Wallet Balance:* Rs. {bal:.2f}")

        # /status
        elif cmd == "/status":
            st = engine.get_status()
            status_str = "🟢 LIVE CONNECTED" if st["logged_in"] else "🔴 DISCONNECTED"
            if st["simulation_mode"]:
                status_str = "🔵 SIMULATION (DEMO)"
            msg = (
                f"*SSC Portal Status:* {status_str}\n"
                f"*Operator:* {st['operator']}\n"
                f"*Wallet Balance:* Rs. {st['wallet_balance']:.2f}\n"
                f"*Mode:* {'Simulation' if st['simulation_mode'] else 'Live'}"
            )
            self.send_message(chat_id, msg)

        # /today
        elif cmd == "/today":
            summary = get_today_summary()
            msg = (
                "📊 *Today's Collection Summary*\n\n"
                f"• *Successful Renewals:* {summary['success_count']}\n"
                f"• *Failed Attempts:* {summary['failed_count']}\n"
                f"• *Cash Collected:* Rs. {summary['total_cash_collected']:.2f}"
            )
            self.send_message(chat_id, msg)

        # /check <STB>
        elif cmd == "/check":
            if len(parts) < 2:
                self.send_message(chat_id, "⚠️ Usage: `/check <STB_NUMBER>`")
                return
            stb = parts[1]
            self.send_message(chat_id, f"🔍 Looking up STB `{stb}`...")
            res = engine.search_customer(stb)
            if res.get("success") and res.get("customer"):
                c = res["customer"]
                msg = (
                    f"👤 *Subscriber:* {c.get('name')}\n"
                    f"📺 *STB:* `{c.get('stb_number')}`\n"
                    f"📦 *Current Plan:* {c.get('current_plan')}\n"
                    f"💰 *Rental:* Rs. {c.get('monthly_rental', 0):.2f}\n"
                    f"📅 *Expiry Date:* {c.get('current_expiry')}\n"
                    f"🏷️ *Status:* `{c.get('status')}`"
                )
                self.send_message(chat_id, msg)
            else:
                self.send_message(chat_id, f"❌ Customer lookup failed: {res.get('message')}")

        # /renew <STB> [amount]
        elif cmd == "/renew":
            if len(parts) < 2:
                self.send_message(chat_id, "⚠️ Usage: `/renew <STB_NUMBER> [amount]`\nExample: `/renew 10023456 350`")
                return
            stb = parts[1]
            amount = float(parts[2]) if len(parts) > 2 else None

            self.send_message(chat_id, f"⚡ Processing cash renewal for STB `{stb}`...")
            cust_res = engine.search_customer(stb)
            cust = cust_res.get("customer", {}) if cust_res.get("success") else {}
            
            plan_name = cust.get("current_plan") or "Standard Pack"
            plan_amount = amount or cust.get("monthly_rental") or 300.0

            result = engine.renew_plan(
                customer_id=cust.get("customer_id") or stb,
                stb_number=cust.get("stb_number") or stb,
                customer_name=cust.get("name"),
                mobile=cust.get("mobile"),
                plan_name=plan_name,
                monthly_price=plan_amount,
                months=1
            )

            if result.get("success"):
                msg = (
                    "✅ *Plan Renewed Successfully!*\n\n"
                    f"👤 *Subscriber:* {cust.get('name', 'Customer')}\n"
                    f"📺 *STB:* `{stb}`\n"
                    f"📦 *Plan:* {plan_name}\n"
                    f"💵 *Cash Paid:* Rs. {plan_amount:.2f}\n"
                    f"📅 *New Expiry:* `{result.get('new_expiry')}`\n"
                    f"💳 *Remaining Wallet:* Rs. {result.get('wallet_balance'):.2f}\n"
                    f"🔖 *Transaction:* `{result.get('transaction_id')}`"
                )
                if result.get("low_balance_alert"):
                    msg += "\n\n⚠️ *Alert: Wallet balance is below threshold!*"
                self.send_message(chat_id, msg)
            else:
                self.send_message(chat_id, f"❌ Renewal Failed:\n{result.get('message')}")

    def run(self):
        if not self.token:
            print("[!] No telegram_bot_token found in config.json.")
            print("[!] To use Telegram bot, create a bot on @BotFather and paste token in config.json.")
            return

        print(f"[*] Starting Telegram Bot polling...")
        while True:
            try:
                url = f"{self.base_url}/getUpdates?offset={self.last_update_id + 1}&timeout=30"
                resp = requests.get(url, timeout=35)
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        self.last_update_id = update.get("update_id", self.last_update_id)
                        msg = update.get("message", {})
                        chat_id = msg.get("chat", {}).get("id")
                        text = msg.get("text", "")
                        
                        # Check access filter if configured
                        if self.allowed_chats and chat_id not in self.allowed_chats:
                            self.send_message(chat_id, "⛔ Unauthorized access.")
                            continue

                        if text.startswith("/"):
                            self.handle_command(chat_id, text)
            except Exception as e:
                logger.error(f"Polling loop error: {e}")
                time.sleep(3)

if __name__ == "__main__":
    bot = SSCTelegramBot()
    bot.run()
