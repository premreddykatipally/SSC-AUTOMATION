import argparse
import sys
import os
from ssc_engine import engine
from database import get_today_summary, get_recent_transactions

def print_banner():
    print("=" * 60)
    print("   SSC (Sri Sai Cable) - Ezybill Billing CLI")
    print("=" * 60)

def cmd_status(args):
    print_banner()
    st = engine.get_status()
    print(f"Portal URL:          {st['portal_url']}")
    print(f"Connection Status:   {'LOGGED IN' if st['logged_in'] else 'DISCONNECTED'}")
    print(f"Mode:                {'SIMULATION (Demo)' if st['simulation_mode'] else 'LIVE PORTAL'}")
    print(f"Operator Account:    {st['operator']}")
    print(f"Wallet Balance:      Rs. {st['wallet_balance']:.2f}")
    if st['wallet_balance'] < st['low_wallet_alert_threshold']:
        print(f"[!] WARNING: Low wallet balance! Threshold is Rs. {st['low_wallet_alert_threshold']:.2f}")
    
    summary = get_today_summary()
    print("-" * 60)
    print(f"Today's Renewals:    {summary['success_count']} successful ({summary['failed_count']} failed)")
    print(f"Cash Collected:      Rs. {summary['total_cash_collected']:.2f}")
    print("=" * 60)

def cmd_lookup(args):
    print(f"[*] Looking up customer: {args.query}...")
    res = engine.search_customer(args.query)
    if not res.get("success"):
        print(f"[-] Error: {res.get('message')}")
        return
    c = res["customer"]
    print("\n--- Subscriber Details ---")
    print(f"Customer Name:   {c.get('name')}")
    print(f"Customer ID:     {c.get('customer_id')}")
    print(f"STB Number:      {c.get('stb_number')}")
    print(f"Mobile Number:   {c.get('mobile')}")
    print(f"Current Plan:    {c.get('current_plan')}")
    print(f"Monthly Rental:  Rs. {c.get('monthly_rental', 0.0):.2f}")
    print(f"Expiry Date:     {c.get('current_expiry')}")
    print(f"Status:          {c.get('status')}")
    print("--------------------------\n")

def cmd_renew(args):
    print(f"[*] Preparing renewal for: {args.target}...")
    lookup = engine.search_customer(args.target)
    cust = lookup.get("customer", {}) if lookup.get("success") else {}

    plan = args.plan or cust.get("current_plan") or "Standard Pack"
    amount = float(args.amount or cust.get("monthly_rental") or 300.0)
    stb = cust.get("stb_number") or args.target

    print(f"Target STB/ID:   {args.target}")
    print(f"Package:         {plan}")
    print(f"Cash Amount:     Rs. {amount:.2f}")

    if not args.yes:
        confirm = input("Confirm cash received and execute renewal? [Y/n]: ").strip().lower()
        if confirm and confirm != 'y':
            print("Renewal cancelled.")
            return

    res = engine.renew_plan(customer_id=cust.get("customer_id") or args.target, stb_number=stb,
                            customer_name=cust.get("name"), mobile=cust.get("mobile"),
                            plan_name=plan, monthly_price=amount, months=1)
    if res.get("success"):
        print("\n[+] SUCCESS: " + res.get("message"))
        print(f"New Expiry Date:     {res.get('new_expiry')}")
        print(f"Amount Due:          Rs. {res.get('cash_due'):.2f}")
        print(f"New Wallet Balance:  Rs. {res.get('wallet_balance'):.2f}")
        if res.get("low_balance_alert"):
            print("[!] Alert: Wallet balance is below safety threshold!")
    else:
        print("\n[-] FAILED: " + res.get("message"))

def cmd_batch(args):
    if not os.path.exists(args.file):
        print(f"[-] File not found: {args.file}")
        return
    with open(args.file, "r") as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    
    print(f"[*] Loaded {len(lines)} customer/STB IDs from {args.file}")
    results = []
    for target in lines:
        lookup = engine.search_customer(target)
        customer = lookup.get("customer", {}) if lookup.get("success") else {}
        if not customer:
            results.append({"stb": target, "status": "FAILED", "message": lookup.get("message", "Customer not found")})
            continue
        result = engine.renew_plan(customer_id=customer["customer_id"], stb_number=customer.get("stb_number"),
                                   customer_name=customer.get("name"), mobile=customer.get("mobile"),
                                   plan_name=customer.get("current_plan"), monthly_price=customer.get("monthly_rental"), months=1)
        results.append({"stb": target, "status": "SUCCESS" if result.get("success") else "FAILED", **result})
    
    print("\n--- Batch Results ---")
    for r in results:
        status_symbol = "[+]" if r["status"] == "SUCCESS" else "[-]"
        print(f"{status_symbol} {r['stb']}: {r['status']} | Expiry: {r.get('new_expiry', '-')} | {r.get('message')}")
    print("---------------------\n")

def cmd_report(args):
    summary = get_today_summary()
    print_banner()
    print(f"Date:                Today")
    print(f"Successful Renewals: {summary['success_count']}")
    print(f"Failed Attempts:     {summary['failed_count']}")
    print(f"Total Cash In:       Rs. {summary['total_cash_collected']:.2f}")
    print("-" * 60)
    print("Recent 10 Transactions:")
    rows = get_recent_transactions(limit=10)
    for r in rows:
        print(f"[{r['timestamp']}] STB:{r['stb_number']} | Rs.{r['plan_amount']} | {r['status']} | Expiry:{r['new_expiry_date']}")
    print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="SSC Ezybill Automated Billing CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # status
    subparsers.add_parser("status", help="Show wallet balance and today's summary")
    
    # lookup
    p_lookup = subparsers.add_parser("lookup", help="Search customer details")
    p_lookup.add_argument("query", help="STB number, Customer ID or Mobile")

    # renew
    p_renew = subparsers.add_parser("renew", help="Renew plan for a single customer")
    p_renew.add_argument("target", help="Customer ID or STB number")
    p_renew.add_argument("--plan", help="Plan name")
    p_renew.add_argument("--amount", type=float, help="Cash amount received (Rs.)")
    p_renew.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # batch
    p_batch = subparsers.add_parser("batch", help="Batch renewal from a text/csv file")
    p_batch.add_argument("file", help="Path to text or CSV file containing IDs (one per line)")
    p_batch.add_argument("--amount", type=float, default=300.0, help="Default plan amount")
    p_batch.add_argument("--plan", default="Standard Pack", help="Default plan name")

    # report
    subparsers.add_parser("report", help="Show today's cash collections and transactions")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "status": cmd_status,
        "lookup": cmd_lookup,
        "renew": cmd_renew,
        "batch": cmd_batch,
        "report": cmd_report
    }
    dispatch[args.command](args)

if __name__ == "__main__":
    main()
