"""Portal recharge preparation and submission.

Only submit_recharge calls createCustomerPost. A preview never changes service.
"""
import html
import json
import re
import secrets
import time

from ssc_engine import engine
from database import search_customers, log_transaction


PLANS = {
    "month": ("1NCF+SSCBPL _Telugu Best Fit", 280.0, 1),
    "six": ("SSCBPL_6 Months TBF Pack", 1680.0, 6),
    "year": ("SSCBPL_1 YEAR TBF PACK", 3080.0, 12),
}

_pending = {}


def _clean(value):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def _grid_row(customer_id, stb_number):
    base = engine.config["portal_url"].rstrip("/")
    form = {
        "customer": "", "last_name": "", "mobile": "", "fname": "", "address": "",
        "vcn": "", "mac": "", "customer_id_number": "", "sbt_search": "Search",
        "services": "4", "backend_setup_id": "-1", "stb_type": "-1",
        "stb_status": "0", "reason_type": "-1", "model": "-1",
        "sub_status": "0", "customer_id_type": "", "csv_download": "0",
        "new_from_dashboard": "0",
    }
    response = engine.session.post(
        base + "/index.php/Das/stbmanagement_list//",
        data={"formData": json.dumps(form), "page": "1", "rows": "1000"}, timeout=30,
    )
    response.raise_for_status()
    rows = response.json().get("rows", [])
    matches = [r for r in rows if str(r.get("customer_id")) == str(customer_id)
               and str(r.get("serial_number")) == str(stb_number)]
    if len(matches) != 1:
        raise ValueError("The selected customer and STB could not be matched to one live portal record. Sync and search again.")
    return matches[0]


def _base_payload(stb, renew=False):
    return [
        ("customer_id", str(stb["customer_id"])),
        ("sellco", str(stb["reseller_id"])),
        ("prev_customer_id", str(stb["customer_id"])),
        ("chk_plugin[]", "4"),
        ("radio_cas_box", "1"),
        ("chk_cas_stock_id[]", str(stb["stock_id"])),
        ("is_customer_temp_reason_exist", "0"),
        ("temp_renew_check", "4" if renew else "0"),
        ("temp_renew_stock_id", str(stb["stock_id"])),
    ]


def _prepare_previous(stb):
    base = engine.config["portal_url"].rstrip("/")
    response = engine.session.get(
        base + "/index.php/Newcustomerwithstb/lastDectivatedPackagesRead",
        params={"customer_id": stb["customer_id"], "reseller_id": stb["reseller_id"],
                "stock_id": stb["stock_id"], "type_service": 4, "plugin_id": 4}, timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    package_html = str(data.get("dectivated_services") or "")
    ids = re.findall(r'<input[^>]*class="[^"]*renew_product_id[^"]*"[^>]*value="(\d+)"', package_html, re.I)
    if not ids:
        raise ValueError("The portal did not identify previous packages for this STB. Select a specific plan instead.")
    return _base_payload(stb, True) + [("product_id[]", value) for value in ids]


def _prepare_new(stb, code):
    base = engine.config["portal_url"].rstrip("/")
    stock = str(stb["stock_id"])
    setup = engine.session.post(
        base + f"/index.php/Newcustomerwithstb/edit_services/{stb['customer_id']}/cas",
        data={
            "array_new_stock_details[0][stock_id]": stock,
            "array_new_stock_details[0][backend_setup_id]": str(stb["backend_setup_id"]),
            "array_new_stock_details[0][stb_type_id]": str(stb["stb_type_id"]),
            "array_new_stock_details[0][serial_number]": str(stb["serial_number"]),
        }, timeout=20,
    )
    setup.raise_for_status()
    if setup.text.strip() != "done":
        raise ValueError("The portal did not accept the package setup.")
    page = engine.session.get(base + "/index.php/Newcustomerwithstb/createCustomerWithStb", timeout=20)
    page.raise_for_status()
    if f'value="{stb["customer_id"]}"' not in page.text:
        raise ValueError("The portal opened a different customer. Recharge was stopped.")
    response = engine.session.post(
        base + "/index.php/Newcustomerwithstb/getProductDetails",
        data={
            "customer_id": str(stb["customer_id"]), "reseller_id": str(stb["reseller_id"]),
            "stock_details[0][stock_id]": stock,
            "stock_details[0][backend_setup_id]": str(stb["backend_setup_id"]),
            "stock_details[0][stb_type_id]": str(stb["stb_type_id"]),
            "stock_details[0][serial_number]": str(stb["serial_number"]),
            "is_new_customer": "0", "plugin_types": "4", "sort_order_val": "1",
            "is_new_box": "1", "isCommercial": "0",
        }, timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") != 1:
        raise ValueError("The portal did not return available packages for this STB.")
    product_html = str(data.get("cas_products_for_act") or "")
    name, _, months = PLANS[code]
    pattern = r'<input[^>]*name="cas_package_id\[' + re.escape(stock) + r'\]\[\]"[^>]*pkg_name="' + re.escape(name) + r'\([^"]+\)"[^>]*>'
    matches = list(re.finditer(pattern, product_html, re.I | re.S))
    if len(matches) != 1:
        raise ValueError("The chosen package is not uniquely available for this STB in the live portal.")
    package_id_match = re.search(r'value="(\d+)"', matches[0].group())
    if not package_id_match:
        raise ValueError("The portal package ID is missing.")
    package_id = package_id_match.group(1)
    row = product_html[matches[0].start():product_html.find("</tr>", matches[0].end())]
    if not row:
        raise ValueError("The portal package details are incomplete.")
    prefix = f"cas_package[{stock}][{package_id}]"
    values = {}
    for key in ("duration", "validity", "quantity", "billtype", "billing_schedule"):
        match = re.search(r'<select[^>]*name\s*=\s*[\'\"]' + re.escape(prefix + f"[{key}]") + r'[\'\"][^>]*>\s*<option[^>]*value=[\'\"]?(\d+)', row, re.I | re.S)
        if not match:
            raise ValueError(f"The portal did not provide {key} for the selected package.")
        values[key] = match.group(1)
    for key in ("startdate", "enddate"):
        match = re.search(r'<input[^>]*name=[\'\"]' + re.escape(prefix + f"[{key}]") + r'[\'\"][^>]*value="([^"]+)"', row, re.I | re.S)
        if not match:
            raise ValueError(f"The portal did not provide {key} for the selected package.")
        values[key] = match.group(1)
    if values["validity"] != str(months) or values["quantity"] != "1":
        raise ValueError("The portal package duration or quantity differs from the selection.")
    return _base_payload(stb) + [(f"cas_package_id[{stock}][]", package_id)] + [
        (prefix + f"[{key}]", value) for key, value in values.items()
    ]


def _prepare_active_extension(stb, requested_months=None):
    """Use the portal's extension review for an already active base package."""
    base = engine.config["portal_url"].rstrip("/")
    stock = str(stb["stock_id"])
    setup = engine.session.post(
        base + f"/index.php/Newcustomerwithstb/edit_services/{stb['customer_id']}/cas",
        data={
            "array_new_stock_details[0][stock_id]": stock,
            "array_new_stock_details[0][backend_setup_id]": str(stb["backend_setup_id"]),
            "array_new_stock_details[0][stb_type_id]": str(stb["stb_type_id"]),
            "array_new_stock_details[0][serial_number]": str(stb["serial_number"]),
        }, timeout=20,
    )
    setup.raise_for_status()
    if setup.text.strip() != "done":
        raise ValueError("The portal did not accept the active service lookup.")
    response = engine.session.post(
        base + "/index.php/Newcustomerwithstb/getProductDetails",
        data={
            "customer_id": str(stb["customer_id"]), "reseller_id": str(stb["reseller_id"]),
            "stock_details[0][stock_id]": stock,
            "stock_details[0][backend_setup_id]": str(stb["backend_setup_id"]),
            "stock_details[0][stb_type_id]": str(stb["stb_type_id"]),
            "stock_details[0][serial_number]": str(stb["serial_number"]),
            "is_new_customer": "0", "plugin_types": "4", "sort_order_val": "1",
            "is_new_box": "1",
        }, timeout=30,
    )
    response.raise_for_status()
    product_html = str(response.json().get("cas_products_for_deact") or "")
    tags = re.findall(r'<a[^>]*class="extend_qty"[^>]*is_base_package="1"[^>]*>', product_html, re.I | re.S)
    if len(tags) != 1:
        raise ValueError("The portal did not identify one active base package for this STB.")
    attrs = {key: quoted or single or bare for key, quoted, single, bare in
             re.findall(r'(\w+)=(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', tags[0])}
    validity_months = int(attrs.get("validity_days") or 0)
    previous = next((plan for plan in PLANS.values()
                     if plan[0].casefold() == attrs.get("product_name", "").strip().casefold()), None)
    if not previous:
        previous = (attrs.get("product_name", "").strip(), None, validity_months)
    if attrs.get("stock_id") != stock or attrs.get("serial_no") != str(stb["serial_number"]):
        raise ValueError("The portal active service does not match the selected STB.")
    if attrs.get("service_type") != "2" or not 1 <= validity_months <= 24:
        raise ValueError("The portal does not offer month-based extension for this active package.")
    months = validity_months if requested_months is None else int(requested_months)
    if not 1 <= months <= 36 or months % validity_months:
        raise ValueError(f"The portal extends this package in {validity_months}-month units. Choose a multiple of {validity_months} months.")
    quantity = months // validity_months
    calc = engine.session.post(
        base + "/index.php/ServiceExtension/calculateServiceExtensionDate",
        data={"int_service_type": attrs["service_type"],
              "int_validity_days": attrs["validity_days"],
              "service_actual_end_date": attrs["product_service_end_date"],
              "int_quantity": str(quantity)}, timeout=20,
    )
    calc.raise_for_status()
    date_result = calc.json()
    if str(date_result.get("status")) != "1" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date_result.get("extension_date") or "")):
        raise ValueError("The portal could not calculate the extension date.")
    payload = {
        "customer_service_id": attrs["cust_service_id"],
        "extend_date": date_result["extension_date"],
        "customer_id": str(stb["customer_id"]),
        "plugin_id": "4", "reseller_id": str(stb["reseller_id"]),
        "service_type_hidden": attrs["service_type"],
        "validity_days_hidden": attrs["validity_days"],
        "service_actual_end_date": attrs["product_service_end_date"],
        "extension_service_months_quantity": str(quantity),
        "extension_service_days_quantity": str(quantity),
    }
    review = engine.session.post(
        base + "/index.php/ServiceExtension/serviceExtensionReview",
        data=payload, timeout=25,
    )
    review.raise_for_status()
    result = review.json()
    details = result.get("msg") if isinstance(result.get("msg"), dict) else {}
    if str(result.get("status")) != "1" or str(details.get("status")) != "1":
        raise ValueError(_clean(result.get("msg")) or "The portal rejected the active extension review.")
    if str(result.get("special_charges_msg", {}).get("serial_number")) != str(stb["serial_number"]):
        raise ValueError("The portal extension review does not match the selected STB.")
    portal_total = float(details.get("total_amount") or 0)
    wallet_debit = float(details.get("mso_share") or 0)
    if portal_total <= 0 or wallet_debit <= 0:
        raise ValueError("The portal extension review returned a zero charge.")
    return payload, previous, portal_total, wallet_debit, date_result["extension_date"], attrs["end_date"], months, quantity


def prepare_recharge(customer_id, stb_number, plan_code, extend_months=None):
    if plan_code not in ("renew", "extend") and plan_code not in PLANS:
        return {"success": False, "message": "Choose a valid plan."}
    with engine.lock:
        try:
            if engine.config.get("simulation_mode"):
                raise ValueError("Direct recharge requires the live portal connection.")
            if not engine.is_logged_in:
                result = engine.login()
                if not result.get("success"):
                    raise ValueError(result.get("message") or "Portal login failed.")
            cached = [r for r in search_customers(str(stb_number))
                      if str(r["customer_id"]) == str(customer_id) and str(r["stb_number"]) == str(stb_number)]
            if len(cached) != 1:
                raise ValueError("This customer is not in the synced directory. Sync and search again.")
            stb = _grid_row(customer_id, stb_number)
            if str(stb.get("is_active")).upper() == "YES":
                requested_months = extend_months if plan_code == "extend" else None
                payload, previous_plan, portal_total, wallet_debit, end_date, current_end, months, quantity = _prepare_active_extension(stb, requested_months)
                if plan_code in PLANS and PLANS[plan_code][0] != previous_plan[0]:
                    raise ValueError(
                        f"This STB has the active {previous_plan[0]} package until {current_end}. "
                        "The portal rejects another base package while it is active. "
                        "Changing now would require deactivating the remaining service."
                    )
                token = secrets.token_urlsafe(32)
                _pending.clear()
                _pending[token] = {"action": "extension", "payload": payload,
                                   "customer": cached[0], "stb": stb, "plan_code": plan_code,
                                   "portal_total": portal_total, "wallet_debit": wallet_debit,
                                   "previous_plan": previous_plan, "months": months,
                                   "cash_price": previous_plan[1] * quantity if previous_plan[1] is not None else None,
                                   "created": time.monotonic()}
                return {"success": True, "token": token, "action": "extension", "portal_total": portal_total,
                        "wallet_debit": wallet_debit,
                        "cash_price": previous_plan[1] * quantity if previous_plan[1] is not None else None,
                        "plan_name": previous_plan[0], "new_expiry": end_date,
                        "selected_months": months, "billing_unit_months": previous_plan[2]}
            if plan_code == "extend":
                raise ValueError("This STB is deactivated. Select a plan to activate instead.")
            payload = _prepare_previous(stb) if plan_code == "renew" else _prepare_new(stb, plan_code)
            base = engine.config["portal_url"].rstrip("/")
            response = engine.session.post(
                base + "/index.php/Newcustomerwithstb/getReviewFormValidation",
                data=payload, timeout=25,
            )
            response.raise_for_status()
            review = response.json()
            cas = _clean(review.get("review_cas_data"))
            total = _clean(review.get("review_total_data"))
            if str(review.get("status")) != "1" or not cas:
                raise ValueError(_clean(review.get("errorMessage")) or "The portal did not validate this recharge.")
            if str(stb["serial_number"]) not in cas:
                raise ValueError("The portal review does not show the selected STB.")
            if plan_code in PLANS and PLANS[plan_code][0].casefold() not in cas.casefold():
                raise ValueError("The portal review does not show the chosen package.")
            match = re.search(r"Total Amount\s*₹?\s*([\d,]+(?:\.\d+)?)", total, re.I)
            if not match:
                raise ValueError("The portal did not return a readable total amount.")
            portal_total = float(match.group(1).replace(",", ""))
            payable_match = re.search(r"Payable\s*₹?\s*([\d,]+(?:\.\d+)?)", total, re.I)
            if not payable_match:
                raise ValueError("The portal did not return a readable wallet debit.")
            wallet_debit = float(payable_match.group(1).replace(",", ""))
            if portal_total <= 0 or wallet_debit <= 0:
                raise ValueError("The portal returned a zero charge.")
            previous_matches = [key for key, plan in PLANS.items()
                                if plan[0].casefold() in cas.casefold()]
            previous_plan = PLANS[previous_matches[0]] if plan_code == "renew" and len(previous_matches) == 1 else None
            if plan_code == "renew" and not previous_plan:
                raise ValueError("The previous package is outside the three priced plans. Select a specific plan to continue.")
            token = secrets.token_urlsafe(32)
            _pending.clear()  # only one operator review at a time for this portal session
            _pending[token] = {"action": "activation", "payload": payload, "customer": cached[0], "stb": stb,
                               "plan_code": plan_code, "portal_total": portal_total,
                               "wallet_debit": wallet_debit,
                               "previous_plan": previous_plan,
                               "created": time.monotonic()}
            return {"success": True, "token": token, "action": "activation", "portal_total": portal_total,
                    "wallet_debit": wallet_debit,
                    "cash_price": PLANS[plan_code][1] if plan_code in PLANS else previous_plan[1] if previous_plan else None,
                    "plan_name": PLANS[plan_code][0] if plan_code in PLANS else previous_plan[0] if previous_plan else "Previous portal packages",
                    "portal_review": cas[:1200]}
        except Exception as exc:
            _pending.clear()
            return {"success": False, "message": str(exc)}


def submit_recharge(token, cash_price=None):
    with engine.lock:
        pending = _pending.get(token)
        if not pending or time.monotonic() - pending["created"] > 600:
            return {"success": False, "message": "The portal quote expired. Select the plan again before activating."}
        if pending["action"] == "extension" and pending.get("cash_price") is None:
            try:
                cash_price = float(cash_price)
            except (TypeError, ValueError):
                return {"success": False, "message": "Enter the cash collection price for this package before extending."}
            if not 0 < cash_price <= 100000:
                return {"success": False, "message": "Enter a valid cash collection price before extending."}
        _pending.pop(token, None)
        base = engine.config["portal_url"].rstrip("/")
        try:
            endpoint = ("/index.php/ServiceExtension/serviceExtensionSubmit"
                        if pending["action"] == "extension"
                        else "/index.php/Newcustomerwithstb/createCustomerPost")
            response = engine.session.post(
                base + endpoint,
                data=pending["payload"], timeout=35,
            )
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            return {"success": False, "message": f"Portal submission result is unknown: {exc}. Check the portal before retrying."}
        message = _clean(result.get("errorMessage"))
        if str(result.get("status")) != "1":
            return {"success": False, "message": message or "The portal rejected the recharge."}
        customer = pending["customer"]
        code = pending["plan_code"]
        chosen_plan = PLANS[code] if code in PLANS else pending["previous_plan"]
        plan_name = chosen_plan[0] if chosen_plan else "Previous portal packages"
        if pending["action"] == "extension":
            cash_price = pending["cash_price"] if pending.get("cash_price") is not None else cash_price
            months = pending["months"]
        else:
            cash_price = chosen_plan[1] if chosen_plan else pending["portal_total"]
            months = chosen_plan[2] if chosen_plan else 0
        wallet_before = engine.wallet_balance
        try:
            engine.fetch_wallet_balance()
        except Exception:
            pass  # portal success must not be reported as a failed recharge
        try:
            log_transaction(customer["customer_id"], customer["stb_number"], customer["name"],
                            customer.get("mobile"), plan_name, months, cash_price,
                            wallet_before, engine.wallet_balance, "SUCCESS",
                            cash_status="PENDING", error_message=message,
                            new_expiry_date=pending["payload"].get("extend_date") if pending["action"] == "extension" else None)
        except Exception:
            return {"success": True, "message": (message or "Portal accepted the recharge.") +
                    " Local cash ledger could not be updated; reconcile it with the portal.",
                    "portal_total": pending["portal_total"], "wallet_debit": pending["wallet_debit"],
                    "cash_due": cash_price}
        return {"success": True, "message": message or "Portal accepted the recharge.",
                "portal_total": pending["portal_total"], "wallet_debit": pending["wallet_debit"],
                "cash_due": cash_price}
