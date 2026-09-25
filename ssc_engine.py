import os
import sys
import re
import io
import time
import json
import hashlib
import threading
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from PIL import Image

os.environ['PYTHONIOENCODING'] = 'utf-8'

try:
    import ddddocr
    _ocr_instance = ddddocr.DdddOcr(show_ad=False)
except Exception as e:
    _ocr_instance = None

from database import log_transaction, log_activity, search_customers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SSCEngine")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class SSCEngine:
    def __init__(self, config_path=None):
        if config_path is None:
            self.config_path = os.path.join(BASE_DIR, "config.json")
        else:
            self.config_path = os.path.abspath(config_path)
        self.load_config()
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        
        self.is_logged_in = False
        self.last_login_time = None
        self.last_login_attempt = None
        self.last_balance_update = None
        self.operator_name = "Not Connected"
        self.wallet_balance = 5450.00 if self.config.get("simulation_mode", False) else 0.0
        self.lock = threading.RLock()
        
        self.keepalive_thread = None
        self.stop_keepalive = threading.Event()
        
    def load_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        else:
            self.config = {
                "portal_url": "https://sms.sscbpl.com",
                "username": "",
                "password": "",
                "simulation_mode": True,
                "session_keepalive_minutes": 10,
                "low_wallet_alert_threshold": 500.0,
                "web_port": 5000,
                "web_host": "127.0.0.1"
            }
            self.save_config()

    def save_config(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def preprocess_captcha(self, img_bytes):
        """Clean noise and isolate dark digits from background waves"""
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        clean = Image.new("L", (w, h), 255)
        pixels = img.load()
        clean_pixels = clean.load()
        for y in range(h):
            for x in range(w):
                r, g, b = pixels[x, y]
                if r < 100 and g < 100 and b < 100:
                    clean_pixels[x, y] = 0
                else:
                    clean_pixels[x, y] = 255
        out = io.BytesIO()
        clean.save(out, format="PNG")
        return out.getvalue()

    def solve_captcha(self, uname="user", max_attempts=5):
        """Fetch, solve with OCR, and validate captcha with the server"""
        if not _ocr_instance:
            logger.error("ddddocr not initialized")
            return None

        base_url = self.config.get("portal_url", "https://sms.sscbpl.com").rstrip("/")
        
        for attempt in range(1, max_attempts + 1):
            try:
                gen_url = f"{base_url}/index.php/captcha/generateCaptcha"
                res = self.session.post(gen_url, data={}, timeout=8)
                match = re.search(r'src=["\']([^"\']+)["\']', res.text)
                if not match:
                    continue
                
                img_url = match.group(1)
                if not img_url.startswith("http"):
                    img_url = urljoin(base_url, img_url)

                img_resp = self.session.get(img_url, timeout=8)
                raw_bytes = img_resp.content

                proc_bytes = self.preprocess_captcha(raw_bytes)
                code = _ocr_instance.classification(proc_bytes).strip()
                if len(code) != 5 or not code.isdigit():
                    code = _ocr_instance.classification(raw_bytes).strip()

                replacements = {'z': '2', 'Z': '2', 'o': '0', 'O': '0', 's': '5', 'S': '5', 'b': '6', 'B': '8', 'l': '1', 'I': '1'}
                clean_code = "".join(replacements.get(c, c) for c in code)

                val_url = f"{base_url}/index.php/captcha/validateCaptcha"
                val_resp = self.session.post(val_url, data={"txtCaptcha": clean_code, "uname": uname}, timeout=8)
                
                if val_resp.text.strip() == "1":
                    logger.info(f"Captcha solved: {clean_code} (attempt {attempt})")
                    return clean_code
                else:
                    time.sleep(0.2)
            except Exception as e:
                logger.error(f"Captcha error: {e}")
                time.sleep(0.3)

        return None

    def login(self, username=None, password=None):
        """Authenticate with sms.sscbpl.com or start simulation"""
        with self.lock:
            self.last_login_attempt = datetime.now()
            uname = username or self.config.get("username", "")
            pwd = password or self.config.get("password", "")

            # If simulation mode is active or no credentials supplied, run simulated session
            if self.config.get("simulation_mode", False) or not uname or not pwd:
                logger.info("Running in Simulation Mode")
                self.is_logged_in = True
                self.operator_name = f"LCO Demo ({uname or 'Operator'})"
                self.wallet_balance = 5450.00
                self.last_login_time = datetime.now()
                return {"success": True, "mode": "simulation", "message": "Connected in Simulation Mode", "wallet": self.wallet_balance, "operator": self.operator_name}

            base_url = self.config.get("portal_url", "https://sms.sscbpl.com").rstrip("/")
            self.is_logged_in = False

            try:
                self.session.get(f"{base_url}/", timeout=10)
                captcha_code = self.solve_captcha(uname=uname)
                if not captcha_code:
                    return {"success": False, "message": "Could not solve captcha automatically. Please retry."}

                pwd_md5 = hashlib.md5(pwd.encode('utf-8')).hexdigest()
                login_url = f"{base_url}/index.php/Welcome/index"
                payload = {
                    "uname": uname,
                    "upassword": pwd_md5,
                    "txtCaptcha": captcha_code,
                    "Submit": "Login"
                }
                
                login_resp = self.session.post(login_url, data=payload, timeout=15, allow_redirects=True)
                
                if "Welcome/index" in login_resp.url:
                    match = re.search(r'class="[^"]*error1[^"]*"[^>]*>(.*?)<', login_resp.text, re.DOTALL)
                    err_msg = match.group(1).strip() if match else "Portal login was not confirmed"
                    return {"success": False, "message": err_msg}

                self.is_logged_in = True
                self.last_login_time = datetime.now()
                self.operator_name = uname
                self.fetch_wallet_balance()
                self.start_keepalive()

                return {
                    "success": True, 
                    "mode": "live", 
                    "message": "Connected successfully to Ezybill Portal", 
                    "wallet": self.wallet_balance,
                    "operator": self.operator_name
                }

            except Exception as e:
                logger.error(f"Login exception: {e}")
                return {"success": False, "message": f"Connection error: {str(e)}"}

    def fetch_wallet_balance(self):
        """Query wallet balance and operator name from the portal dashboard"""
        if self.config.get("simulation_mode", False):
            self.last_balance_update = datetime.now()
            return self.wallet_balance

        base_url = self.config.get("portal_url", "https://sms.sscbpl.com").rstrip("/")
        try:
            resp = self.session.get(f"{base_url}/index.php/Dashboard/index", timeout=12)
            if "Welcome/index" in resp.url or "login" in resp.url.lower():
                logger.info("Session expired on portal; waiting before another login attempt")
                self.is_logged_in = False
                return self.wallet_balance

            # 1. Available Balance extraction (e.g. INR 905.35 right before Available Balance)
            balance_match = re.search(r'INR\s*([0-9,]+\.?[0-9]*)\s*</h5>\s*</div>\s*<div[^>]*>\s*<h6[^>]*>\s*Available Balance', resp.text, re.IGNORECASE)
            if not balance_match:
                balance_match = re.search(r'INR\s*([0-9,]+\.?[0-9]*)', resp.text, re.IGNORECASE)
            if balance_match:
                val = float(balance_match.group(1).replace(",", ""))
                self.wallet_balance = val
                self.last_balance_update = datetime.now()

            # 2. Operator / Network Name extraction (e.g. Welcome : Ravindher Reddy.Chandu Cable Network.)
            op_match = re.search(r'Welcome\s*:\s*([^<]+)', resp.text, re.IGNORECASE)
            if op_match:
                self.operator_name = op_match.group(1).strip()

            return self.wallet_balance
        except Exception as e:
            logger.warning(f"Could not parse wallet balance: {e}")
            return self.wallet_balance

    def start_keepalive(self):
        if self.keepalive_thread and self.keepalive_thread.is_alive():
            return
            
        self.stop_keepalive.clear()
        # Live background sync interval (15 seconds keeps balance fresh & session alive)
        interval = 15

        def run():
            while not self.stop_keepalive.wait(interval):
                if not self.is_logged_in or self.config.get("simulation_mode", False):
                    continue
                try:
                    self.fetch_wallet_balance()
                except Exception as e:
                    logger.warning(f"Live sync failed: {e}")

        self.keepalive_thread = threading.Thread(target=run, daemon=True)
        self.keepalive_thread.start()

    def sync_customers(self):
        """Fetch all live subscribers from Ezybill STB Management and save to SQLite"""
        if self.config.get("simulation_mode", False):
            return {"success": True, "message": "Simulation mode active"}

        if not self.is_logged_in:
            login_res = self.login()
            if not login_res.get("success"):
                return {"success": False, "message": f"Login failed: {login_res.get('message')}"}

        base_url = self.config.get("portal_url", "https://sms.sscbpl.com").rstrip("/")
        form_data = {
            "customer": "", "last_name": "", "mobile": "", "fname": "", "address": "",
            "vcn": "", "mac": "", "customer_id_number": "", "sbt_search": "Search",
            "services": "4", "backend_setup_id": "-1", "stb_type": "-1", "stb_status": "0",
            "reason_type": "-1", "model": "-1", "sub_status": "0", "customer_id_type": "",
            "csv_download": "0", "new_from_dashboard": "0"
        }

        payload = {
            "formData": json.dumps(form_data),
            "page": "1",
            "rows": "1000"
        }

        url = f"{base_url}/index.php/Das/stbmanagement_list//"
        try:
            resp = self.session.post(url, data=payload, timeout=30)
            if "Welcome/index" in resp.url:
                self.is_logged_in = False
                self.login()
                resp = self.session.post(url, data=payload, timeout=30)

            data = json.loads(resp.text.strip())
            rows = data.get("rows", [])
            if not rows:
                return {"success": False, "message": "No STB records returned by portal"}

            records = []
            for r in rows:
                stb = str(r.get("serial_number") or "").strip()
                if not stb:
                    continue
                cust_id = str(r.get("customer_id") or r.get("cust_identification") or stb).strip()
                vc = str(r.get("vc_number") or "").strip()
                name = str(r.get("customer_name") or "").strip()
                mobile = str(r.get("mobile_no") or "").strip()
                if mobile.startswith("91") and len(mobile) == 12:
                    mobile = mobile[2:]
                addr = str(r.get("installation_address") or r.get("location_name") or "").strip()
                is_act = str(r.get("is_active") or "").upper() == "YES"
                status_val = "ACTIVE" if is_act else "DEACTIVE"
                # STB Management exposes activate_date (installation/activation),
                # not the subscription expiry date.
                prod = str(r.get("product_name") or "").strip()
                plan_name = f"SSC Pack {prod}" if prod else "Standard Base Pack"

                display_name = name if name else f"Unassigned ({stb[-6:]})"
                records.append({
                    "customer_id": cust_id,
                    "stb_number": stb,
                    "vc_number": vc,
                    "name": display_name,
                    "mobile": mobile,
                    "address": addr,
                    "current_plan": plan_name,
                    "current_expiry": None,
                    "monthly_rental": 300.00,
                    "status": status_val
                })

            from database import upsert_customers, get_customer_stats
            upsert_customers(records)
            stats = get_customer_stats()
            logger.info(f"Live STB sync complete: {stats}")
            return {
                "success": True,
                "message": f"Successfully synced {len(records)} subscribers from Ezybill STB Management!",
                "total": stats.get("total", len(records)),
                "active": stats.get("active", 0),
                "deactive": stats.get("deactive", 0),
                "assigned": stats.get("assigned", 0)
            }
        except Exception as e:
            logger.error(f"Error syncing STBs: {e}")
            return {"success": False, "message": f"Sync error: {str(e)}"}

    def search_customer(self, query):
        """Find customer in local database or portal"""
        query = str(query).strip()
        if not query:
            return {"success": False, "message": "Search query cannot be empty"}

        # 1. Search in local database directory first
        cached = search_customers(query)
        if cached:
            c = cached[0]
            plans = [
                {"name": c.get("current_plan") or "SSC Telugu Super HD", "price": float(c.get("monthly_rental") or 350.0)},
                {"name": "SSC Telugu Gold Pack", "price": 280.0},
                {"name": "SSC Sports & Movies HD", "price": 420.0},
                {"name": "SSC Standard Digital", "price": 220.0}
            ]
            return {
                "success": True,
                "matches": cached,
                "customer": {
                    "customer_id": c["customer_id"],
                    "stb_number": c["stb_number"],
                    "name": c["name"],
                    "mobile": c.get("mobile", ""),
                    "address": c.get("address", ""),
                    "current_plan": c.get("current_plan") or "SSC Telugu Super HD",
                    "monthly_rental": float(c.get("monthly_rental") or 350.0),
                    "current_expiry": c.get("current_expiry"),
                    "status": c.get("status") or "UNKNOWN",
                    "available_plans": plans
                }
            }

        # 2. Simulation Mode fallback
        if self.config.get("simulation_mode", False):
            stb = query if query.isdigit() and len(query) >= 6 else "100" + "".join([str(ord(ch)%10) for ch in query])[:6]
            plans = [
                {"name": "SSC Telugu Super HD", "price": 350.0},
                {"name": "SSC Telugu Gold Pack", "price": 280.0},
                {"name": "SSC Sports & Movies HD", "price": 420.0},
                {"name": "SSC Standard Digital", "price": 220.0}
            ]
            selected_plan = plans[hash(query) % len(plans)]
            fake_expiry = (datetime.now() + timedelta(days=-1)).strftime("%Y-%m-%d")

            return {
                "success": True,
                "customer": {
                    "customer_id": "SSC-" + query[-5:] if len(query) >= 5 else f"SSC-{query}01",
                    "stb_number": stb,
                    "name": f"Subscriber ({query})",
                    "mobile": f"98480{query[-5:]}" if len(query) >= 5 else "9848012345",
                    "address": "Local Cable Area",
                    "current_plan": selected_plan["name"],
                    "monthly_rental": selected_plan["price"],
                    "current_expiry": fake_expiry,
                    "status": "EXPIRED",
                    "available_plans": plans
                }
            }

        # Live renewals require a subscriber that was actually synced from the portal.
        return {"success": False, "message": "Subscriber not found locally. Sync subscribers from the portal and search again."}

    def get_status(self, force_refresh=False):
        if self.is_logged_in and not self.config.get("simulation_mode", False):
            is_stale = self.last_balance_update is None or (datetime.now() - self.last_balance_update).total_seconds() > 10
            if force_refresh or is_stale:
                try:
                    self.fetch_wallet_balance()
                except Exception as e:
                    logger.warning(f"Status refresh balance failed: {e}")

        return {
            "logged_in": self.is_logged_in,
            "operator": self.operator_name,
            "wallet_balance": self.wallet_balance,
            "simulation_mode": self.config.get("simulation_mode", False),
            "portal_url": self.config.get("portal_url", "https://sms.sscbpl.com"),
            "username": self.config.get("username", ""),
            "last_login": self.last_login_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_login_time else "Never",
            "last_balance_update": self.last_balance_update.strftime("%H:%M:%S") if self.last_balance_update else "Syncing...",
            "low_wallet_alert_threshold": self.config.get("low_wallet_alert_threshold", 500.0)
        }

engine = SSCEngine()
