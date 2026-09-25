import sqlite3
import os
from datetime import datetime

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "ssc_billing.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Transactions table with cash collection tracking
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        customer_id TEXT NOT NULL,
        stb_number TEXT,
        customer_name TEXT,
        mobile TEXT,
        plan_name TEXT,
        months_renewed INTEGER DEFAULT 1,
        plan_amount REAL,
        cash_status TEXT DEFAULT 'PENDING',  -- 'PENDING' or 'COLLECTED'
        collected_at TEXT,
        wallet_balance_before REAL,
        wallet_balance_after REAL,
        status TEXT NOT NULL,                -- 'SUCCESS' or 'FAILED'
        error_message TEXT,
        new_expiry_date TEXT,
        ezybill_ref TEXT,
        notes TEXT
    )
    """)
    
    # Check if columns exist for backwards compatibility
    cursor.execute("PRAGMA table_info(transactions)")
    columns = [row["name"] for row in cursor.fetchall()]
    if "cash_status" not in columns:
        cursor.execute("ALTER TABLE transactions ADD COLUMN cash_status TEXT DEFAULT 'PENDING'")
    if "months_renewed" not in columns:
        cursor.execute("ALTER TABLE transactions ADD COLUMN months_renewed INTEGER DEFAULT 1")
    if "collected_at" not in columns:
        cursor.execute("ALTER TABLE transactions ADD COLUMN collected_at TEXT")
    if "notes" not in columns:
        cursor.execute("ALTER TABLE transactions ADD COLUMN notes TEXT")

    # Customer directory for rapid autocomplete & live STB management
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id TEXT UNIQUE NOT NULL,
        stb_number TEXT,
        vc_number TEXT,
        name TEXT,
        mobile TEXT,
        address TEXT,
        current_plan TEXT,
        current_expiry TEXT,
        monthly_rental REAL,
        status TEXT DEFAULT 'ACTIVE',
        last_renewed TEXT
    )
    """)

    cursor.execute("PRAGMA table_info(customers)")
    cust_cols = [row["name"] for row in cursor.fetchall()]
    if "vc_number" not in cust_cols:
        cursor.execute("ALTER TABLE customers ADD COLUMN vc_number TEXT")
    if "status" not in cust_cols:
        cursor.execute("ALTER TABLE customers ADD COLUMN status TEXT DEFAULT 'ACTIVE'")

    # Clean out any old dummy demo sample customers
    cursor.execute("DELETE FROM customers WHERE customer_id LIKE 'SSC-%'")

    conn.commit()
    conn.close()

def log_activity(level, action, details=""):
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        level TEXT NOT NULL,
        action TEXT NOT NULL,
        details TEXT
    )
    """)
    cursor.execute("INSERT INTO activity_logs (timestamp, level, action, details) VALUES (?, ?, ?, ?)",
                   (now, level, action, details))
    conn.commit()
    conn.close()

def log_transaction(customer_id, stb_number, customer_name, mobile, plan_name, 
                    months_renewed, plan_amount, wallet_before, wallet_after, 
                    status, cash_status="PENDING", error_message=None, new_expiry_date=None, ezybill_ref=None):
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT INTO transactions (
        timestamp, customer_id, stb_number, customer_name, mobile, 
        plan_name, months_renewed, plan_amount, cash_status, 
        wallet_balance_before, wallet_balance_after, status, error_message, new_expiry_date, ezybill_ref
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (now, str(customer_id), str(stb_number or ''), str(customer_name or ''), 
          str(mobile or ''), str(plan_name or ''), int(months_renewed or 1), 
          float(plan_amount or 0.0), cash_status, float(wallet_before or 0.0), 
          float(wallet_after or 0.0), status, error_message, new_expiry_date, ezybill_ref))
    
    # Also update customer directory
    if customer_id and status == "SUCCESS":
        cursor.execute("""
        INSERT INTO customers (customer_id, stb_number, name, mobile, current_plan, current_expiry, last_renewed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            stb_number = COALESCE(excluded.stb_number, customers.stb_number),
            name = COALESCE(excluded.name, customers.name),
            mobile = COALESCE(excluded.mobile, customers.mobile),
            current_plan = COALESCE(excluded.current_plan, customers.current_plan),
            current_expiry = COALESCE(excluded.current_expiry, customers.current_expiry),
            last_renewed = excluded.last_renewed
        """, (str(customer_id), str(stb_number or ''), str(customer_name or ''), 
              str(mobile or ''), str(plan_name or ''), str(new_expiry_date or ''), now))
        
    conn.commit()
    txn_id = cursor.lastrowid
    conn.close()
    return txn_id

def toggle_cash_status(transaction_id, status=None):
    """Toggle or set cash status to 'COLLECTED' or 'PENDING'"""
    conn = get_db()
    cursor = conn.cursor()
    if status not in (None, "COLLECTED", "PENDING"):
        conn.close()
        return None
    
    if status is None:
        cursor.execute("SELECT cash_status FROM transactions WHERE id = ?", (transaction_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
        current = row["cash_status"]
        new_status = "COLLECTED" if current == "PENDING" else "PENDING"
    else:
        new_status = status

    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if new_status == "COLLECTED" else None
    
    cursor.execute("""
    UPDATE transactions 
    SET cash_status = ?, collected_at = ?
    WHERE id = ? AND status = 'SUCCESS'
    """, (new_status, collected_at, transaction_id))
    
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return new_status if changed else None

def get_live_database(filter_status="ALL", limit=200):
    """Fetch live customer transactions with cash collection status"""
    conn = get_db()
    cursor = conn.cursor()
    
    query = "SELECT * FROM transactions WHERE status = 'SUCCESS'"
    params = []
    
    if filter_status == "PENDING":
        query += " AND cash_status = 'PENDING'"
    elif filter_status == "COLLECTED":
        query += " AND cash_status = 'COLLECTED'"
        
    query += " ORDER BY id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def get_collection_summary():
    """Calculate pending cash vs collected cash totals"""
    conn = get_db()
    cursor = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute("""
    SELECT 
        COUNT(*) as total_renewals,
        COALESCE(SUM(CASE WHEN cash_status = 'PENDING' THEN 1 ELSE 0 END), 0) as pending_count,
        COALESCE(SUM(CASE WHEN cash_status = 'COLLECTED' THEN 1 ELSE 0 END), 0) as collected_count,
        COALESCE(SUM(CASE WHEN cash_status = 'COLLECTED' THEN plan_amount ELSE 0 END), 0) as cash_collected_total,
        COALESCE(SUM(CASE WHEN cash_status = 'PENDING' THEN plan_amount ELSE 0 END), 0) as cash_pending_total,
        COALESCE(SUM(CASE WHEN timestamp LIKE ? AND cash_status = 'COLLECTED' THEN plan_amount ELSE 0 END), 0) as cash_collected_today,
        COALESCE(SUM(CASE WHEN timestamp LIKE ? AND cash_status = 'PENDING' THEN plan_amount ELSE 0 END), 0) as cash_pending_today,
        COALESCE(SUM(CASE WHEN timestamp LIKE ? THEN 1 ELSE 0 END), 0) as renewals_today
    FROM transactions 
    WHERE status = 'SUCCESS'
    """, (f"{today_str}%", f"{today_str}%", f"{today_str}%"))
    
    row = dict(cursor.fetchone())
    conn.close()
    return row

def get_today_summary():
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    row = conn.execute("""SELECT
        COALESCE(SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END), 0) success_count,
        COALESCE(SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END), 0) failed_count,
        COALESCE(SUM(CASE WHEN status = 'SUCCESS' AND cash_status = 'COLLECTED' THEN plan_amount ELSE 0 END), 0) total_cash_collected
        FROM transactions WHERE timestamp LIKE ?""", (today,)).fetchone()
    result = dict(row)
    conn.close()
    return result

def get_recent_transactions(limit=10):
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit,))]
    conn.close()
    return rows

def get_customer_stats():
    """Return summary statistics of subscribers in database"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT 
        COUNT(*) as total,
        COALESCE(SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END), 0) as active,
        COALESCE(SUM(CASE WHEN status = 'DEACTIVE' THEN 1 ELSE 0 END), 0) as deactive,
        COALESCE(SUM(CASE WHEN name NOT LIKE 'Unassigned%' THEN 1 ELSE 0 END), 0) as assigned
    FROM customers
    """)
    stats = dict(cursor.fetchone())
    conn.close()
    return stats

def search_customers(query, limit=None):
    """Search directory for instant match by STB, VC, Name, Mobile, or Customer ID"""
    conn = get_db()
    cursor = conn.cursor()
    q = f"%{query}%"
    sql = """
    SELECT * FROM customers 
    WHERE customer_id LIKE ? 
       OR stb_number LIKE ? 
       OR vc_number LIKE ?
       OR mobile LIKE ? 
       OR name LIKE ?
       OR address LIKE ?
    ORDER BY CASE WHEN status = 'ACTIVE' THEN 0 ELSE 1 END, name ASC
    """
    params = [q, q, q, q, q, q]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor.execute(sql, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def upsert_customers(customer_records):
    """Bulk upsert live subscriber records from portal STB management"""
    if not customer_records:
        return 0
    conn = get_db()
    cursor = conn.cursor()
    cursor.executemany("""
    INSERT INTO customers (customer_id, stb_number, vc_number, name, mobile, address, current_plan, current_expiry, monthly_rental, status)
    VALUES (:customer_id, :stb_number, :vc_number, :name, :mobile, :address, :current_plan, :current_expiry, :monthly_rental, :status)
    ON CONFLICT(customer_id) DO UPDATE SET
        stb_number=excluded.stb_number,
        vc_number=excluded.vc_number,
        name=excluded.name,
        mobile=excluded.mobile,
        address=excluded.address,
        current_plan=excluded.current_plan,
        current_expiry=excluded.current_expiry,
        monthly_rental=excluded.monthly_rental,
        status=excluded.status
    """, customer_records)
    conn.commit()
    count = cursor.rowcount
    conn.close()
    return count

# Initialize DB tables
init_db()
