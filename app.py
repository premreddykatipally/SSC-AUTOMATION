import os
import sys
import csv
import io
import json
import ipaddress
import secrets
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response

from ssc_engine import engine
from recharge import prepare_recharge, submit_recharge
from database import (
    get_live_database, 
    toggle_cash_status, 
    get_collection_summary, 
    search_customers,
    get_customer_stats,
    get_db,
    log_activity
)

app = Flask(__name__, template_folder="templates")
app.secret_key = "ssc-billing-automation-cash-portal"

@app.before_request
def protect_dashboard():
    # A LAN listener must have its own password; portal credentials are never used here.
    remote = ipaddress.ip_address(request.remote_addr or "127.0.0.1")
    password = os.environ.get("SSC_APP_PASSWORD", "")
    if not remote.is_loopback:
        auth = request.authorization
        if not password or not auth or not secrets.compare_digest(auth.password or "", password):
            return Response("Dashboard password required", 401,
                            {"WWW-Authenticate": 'Basic realm="SSC Dashboard"'})
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify({"success": False, "message": "Invalid request origin"}), 403

@app.after_request
def add_cache_control_headers(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status", methods=["GET"])
def api_status():
    refresh_param = request.args.get("refresh", "").lower() in ["1", "true"]
    retry_due = engine.last_login_attempt is None or (datetime.now() - engine.last_login_attempt).total_seconds() >= 60
    if not engine.is_logged_in and retry_due and engine.config.get("username") and engine.config.get("password") and not engine.config.get("simulation_mode"):
        engine.login()
    status = engine.get_status(force_refresh=refresh_param)
    summary = get_collection_summary()
    subscribers = get_customer_stats()
    return jsonify({
        "status": status,
        "summary": summary,
        "subscribers": subscribers
    })

@app.route("/api/sync-customers", methods=["POST", "GET"])
def api_sync_customers():
    """Trigger on-demand sync from Ezybill STB Management"""
    result = engine.sync_customers()
    return jsonify(result)

@app.route("/api/subscribers", methods=["GET"])
def api_subscribers():
    status_filter = request.args.get("status", "ALL").upper()
    query = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))

    conn = get_db()
    cursor = conn.cursor()
    sql = "SELECT * FROM customers WHERE 1=1"
    params = []

    if status_filter in ["ACTIVE", "DEACTIVE"]:
        sql += " AND status = ?"
        params.append(status_filter)

    if query:
        q = f"%{query}%"
        sql += " AND (name LIKE ? OR stb_number LIKE ? OR vc_number LIKE ? OR mobile LIKE ? OR address LIKE ?)"
        params.extend([q, q, q, q, q])

    sql += " ORDER BY CASE WHEN status = 'ACTIVE' THEN 0 ELSE 1 END, name ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(sql, params)
    rows = [dict(r) for r in cursor.fetchall()]
    stats = get_customer_stats()
    conn.close()

    return jsonify({
        "subscribers": rows,
        "stats": stats
    })

@app.route("/api/customer/search", methods=["GET"])
def api_search():
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify({"success": False, "message": "Search query cannot be empty"})
    
    res = engine.search_customer(query)
    return jsonify(res)

@app.route("/api/renew", methods=["POST"])
def api_renew():
    data = request.get_json(silent=True) or {}
    return jsonify(submit_recharge(str(data.get("token") or ""), data.get("cash_price")))


@app.route("/api/recharge/prepare", methods=["POST"])
def api_prepare_recharge():
    data = request.get_json(silent=True) or {}
    return jsonify(prepare_recharge(str(data.get("customer_id") or ""),
                                    str(data.get("stb_number") or ""),
                                    str(data.get("plan_code") or ""),
                                    data.get("extend_months")))

@app.route("/api/live-database", methods=["GET"])
def api_live_database():
    filter_status = request.args.get("filter", "ALL").upper()
    rows = get_live_database(filter_status=filter_status, limit=None)
    summary = get_collection_summary()
    return jsonify({
        "customers": rows,
        "summary": summary
    })

@app.route("/api/toggle-cash", methods=["POST"])
def api_toggle_cash():
    data = request.get_json() or {}
    txn_id = data.get("id")
    target_status = data.get("status") # 'COLLECTED' or 'PENDING' or None (toggle)

    if not txn_id or target_status not in (None, "COLLECTED", "PENDING"):
        return jsonify({"success": False, "message": "Valid transaction ID and cash status required"}), 400

    new_status = toggle_cash_status(txn_id, status=target_status)
    if new_status is None:
        return jsonify({"success": False, "message": "Transaction record not found"})

    summary = get_collection_summary()
    return jsonify({
        "success": True,
        "id": txn_id,
        "cash_status": new_status,
        "summary": summary
    })

@app.route("/api/export-csv", methods=["GET"])
def api_export_csv():
    rows = get_live_database(filter_status="ALL", limit=None)
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow([
        "ID", "Renewal Time", "Customer Name", "STB Number", "Mobile", 
        "Plan Name", "Months", "Cash Amount (Rs.)", "Cash Status", 
        "Collected Time", "New Expiry Date", "Ezybill Ref"
    ])
    
    for r in rows:
        writer.writerow([
            r.get("id"), r.get("timestamp"), r.get("customer_name"), r.get("stb_number"),
            r.get("mobile"), r.get("plan_name"), r.get("months_renewed"), r.get("plan_amount"),
            r.get("cash_status"), r.get("collected_at") or "Pending", 
            r.get("new_expiry_date"), r.get("ezybill_ref")
        ])
        
    output.seek(0)
    filename = f"ssc_live_cash_ledger_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = data.get("username")
    password = data.get("password")
    simulation = data.get("simulation_mode")

    if simulation is not None:
        engine.config["simulation_mode"] = bool(simulation)
    if username:
        engine.config["username"] = username
    if password:
        engine.config["password"] = password

    engine.save_config()
    result = engine.login(username=username, password=password)
    return jsonify(result)

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json() or {}
        previous = {key: engine.config.get(key) for key in ["portal_url", "username", "password", "simulation_mode"]}
        for key in ["portal_url", "username", "password", "simulation_mode", 
                    "low_wallet_alert_threshold"]:
            if key in data:
                engine.config[key] = data[key]
        if any(engine.config.get(key) != value for key, value in previous.items()):
            with engine.lock:
                engine.is_logged_in = False
                engine.last_login_time = None
                engine.last_balance_update = None
                engine.operator_name = "Not Connected"
                engine.wallet_balance = 5450.0 if engine.config.get("simulation_mode") else 0.0
                engine.session.cookies.clear()
        engine.save_config()
        return jsonify({"success": True, "message": "Settings updated"})
    else:
        cfg = dict(engine.config)
        if cfg.get("password"):
            cfg["password"] = "••••••••"
        return jsonify(cfg)

if __name__ == "__main__":
    port = engine.config.get("web_port", 5000)
    host = engine.config.get("web_host", "0.0.0.0")
    print(f"[*] Starting SSC Ezybill Cash Billing Automation on http://localhost:{port}")
    app.run(host=host, port=port, debug=False)
