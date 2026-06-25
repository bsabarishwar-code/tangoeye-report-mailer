from flask import Flask, request, jsonify
import threading
import requests
from datetime import datetime, timezone, timedelta
import csv
import base64
import os
import io

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
CRON_SECRET    = os.environ.get("CRON_SECRET",          "changeme")
API_TOKEN      = os.environ.get("DASHBOARD_API_TOKEN",  "9f0a75c6-097a-422c-883c-5946fb2d0d2f")
BREVO_API_KEY  = os.environ.get("BREVO_API_KEY",        "")
SENDER_EMAIL   = os.environ.get("SENDER_EMAIL",         "tangoeye.ops@gmail.com")
SENDER_NAME    = os.environ.get("SENDER_NAME",          "TangoEye Reports")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAILS",      "sabarishwar@tangotech.co.in,ravichandran@tangotech.co.in,lokesh@tangotech.co.in")
ALERT_EMAIL    = "sabarishwar@tangotech.co.in"

OS_HOST  = "search-tango-prod-7dsufau4cxzttx6yt7dqc3rzme.ap-south-1.es.amazonaws.com"
OS_USER  = os.environ.get("OPENSEARCH_USER", "ravi")
OS_PASS  = os.environ.get("OPENSEARCH_PASS", "T@ng0#2024")
OS_INDEX = "tango-audit-activity-logs"

API_BASE = "https://dashboard-api.tangoeye.ai/v3"

ist = timezone(timedelta(hours=5, minutes=30))

EXCLUDED_IDS = {"201", "425", "458", "468", "520"}
PRIORITY_IDS = {"193", "455", "185", "460", "94", "469", "95", "409", "4", "11", "322", "434"}

KEY_CLIENT_NAMES = [
    "Owndays", "Rivoli", "The Souled Store", "Woodenstreet Furnitures",
    "TCNS", "Ample", "Duroflex", "Cashify", "Nykaa", "Lenskart", "HP", "Cult sport",
    "Sunny diamonds", "Licious", "truebrowns", "Chaicup", "Peachmode", "Nestasia",
    "Flyberry", "Smytten", "Comet", "The Pant Project", "Enrich", "Go Colors",
    "Spykar", "Aukera Jewellery", "PowerSports", "Virgio", "Sundora Beauty",
    "Ajmal Perfume", "House of Anita Dongre", "JFA", "Le Petit Lunetier Lyon",
    "Columbia sportswear", "Giva", "Lyskraft", "Neeman's", "Sangeetha",
    "AL AMIN OPTICALS GROUP", "The Indian Garage CO", "Bewakoof", "Frido",
    "WONDERCHEF", "Starlink", "Eluno", "Safari", "Nike", "Zigly", "Nobero",
    "A2B", "Sweet Dreams", "Uni Seoul", "DailyObjects", "KPN Fresh",
    "Challani Jewellery Mart", "Alan Scott Retail Limited", "HP Connect", "Inc5",
    "Ethera Diamonds",
]
KEY_NAMES_LOWER = {n.lower() for n in KEY_CLIENT_NAMES}
GO_COLORS_LOWER = "go colors"

STATUS_MAP = {"active": "Active", "deactive": "Deactivated", "suspended": "Hold"}

# In-memory set to prevent duplicate midday emails on the same date
_midday_sent = set()
_midday_lock = threading.Lock()


# ── BREVO HELPER ──────────────────────────────────────────────────────────────
def send_brevo(subject, html_body, to_csv, csv_bytes=None, csv_filename=None):
    to_list = [{"email": e.strip()} for e in to_csv.split(",") if e.strip()]
    payload = {
        "sender":      {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to":          to_list,
        "subject":     subject,
        "htmlContent": html_body,
    }
    if csv_bytes and csv_filename:
        payload["attachment"] = [{"content": base64.b64encode(csv_bytes).decode(),
                                  "name":    csv_filename}]
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    resp.raise_for_status()


def rows_to_csv_bytes(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Client Name", "Client ID", "Client Status",
                     "Report Sent Time", "Sent By", "Report Status"])
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ── SHARED: DASHBOARD FETCH (all clients, any date) ──────────────────────────
def fetch_dashboard_all(input_date, input_date_iso):
    auth  = {"Authorization": f"Bearer {API_TOKEN}"}
    jsonh = {**auth, "Content-Type": "application/json"}

    r = requests.get(f"{API_BASE}/client/get-clients", headers=auth, timeout=30)
    r.raise_for_status()
    all_clients = r.json()["data"]["result"]
    all_ids_str = [str(c["clientId"]) for c in all_clients]
    client_map  = {str(c["clientId"]): c["clientName"] for c in all_clients}
    print(f"  Clients: {len(all_clients)}")

    status_map = {}
    offset, limit = 1, 500
    while True:
        r2 = requests.post(
            f"{API_BASE}/report/client-list-table", headers=jsonh,
            json={"fromDate": input_date_iso, "toDate": input_date_iso,
                  "clientId": all_ids_str, "reportStatus": "",
                  "sortBy": -1, "limit": limit, "offset": offset},
            timeout=30,
        )
        r2.raise_for_status()
        if r2.status_code == 204 or not r2.content:
            break
        body   = r2.json()
        result = body.get("data", {}).get("result", [])
        total  = body.get("data", {}).get("count", 0)
        for rec in result:
            status_map[str(rec["tangoId"])] = rec
        print(f"  Status: {len(status_map)}/{total}")
        if not result or len(status_map) >= total:
            break
        offset += limit

    sent_ids = [cid for cid, rec in status_map.items() if rec.get("reportStatus") == "sent"]
    send_log  = {}
    for cid in sent_ids:
        try:
            r3 = requests.post(
                f"{API_BASE}/report/get-report-log", headers=jsonh,
                json={"clientId": cid, "fileDate": input_date},
                timeout=30,
            )
            r3.raise_for_status()
            for log in r3.json().get("data", {}).get("result", []):
                if log.get("logSubType") != "sendReport":
                    continue
                if log.get("logData", {}).get("fileDate") != input_date:
                    continue
                prev = send_log.get(cid)
                if prev is None or log["createdAt"] > prev["createdAt"]:
                    send_log[cid] = log
        except Exception as e:
            print(f"  Warning: log fetch failed for {cid}: {e}")
    print(f"  Send logs: {len(send_log)}")

    return client_map, status_map, send_log


# ── SHARED: OPENSEARCH FETCH (all clients, any date) ─────────────────────────
def fetch_opensearch_all(input_date):
    from opensearchpy import OpenSearch, RequestsHttpConnection
    osc = OpenSearch(
        hosts=[{"host": OS_HOST, "port": 443}],
        http_auth=(OS_USER, OS_PASS),
        use_ssl=True, verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

    def query_logs(subtype):
        resp = osc.search(index=OS_INDEX, body={
            "query": {"bool": {"must": [
                {"term": {"logType.keyword":         "report"}},
                {"term": {"logSubType.keyword":       subtype}},
                {"term": {"logData.fileDate.keyword": input_date}},
            ]}},
            "sort": [{"createdAt": {"order": "desc"}}],
            "size": 2000,
        })
        return [h["_source"] for h in resp["hits"]["hits"]]

    def epoch_to_ist(ms):
        return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                + timedelta(hours=5, minutes=30)).strftime("%H:%M:%S")

    send_logs     = query_logs("sendReport")
    generate_logs = query_logs("generateReport")
    print(f"  OS sendReport: {len(send_logs)}  generateReport: {len(generate_logs)}")

    client_info = {}
    for log in generate_logs:
        ld  = log.get("logData", {})
        cid = str(ld.get("clientId", ""))
        if not cid or cid in EXCLUDED_IDS:
            continue
        if cid not in client_info:
            client_info[cid] = {"name": ld.get("reportName", f"Client {cid}"),
                                 "time_val": "-", "sent_by": "-", "has_send": False}

    for log in send_logs:
        ld  = log.get("logData", {})
        cid = str(ld.get("clientId", ""))
        if not cid or cid in EXCLUDED_IDS:
            continue
        if cid in client_info and client_info[cid]["has_send"]:
            continue
        ms       = log.get("createdAt", 0)
        time_val = epoch_to_ist(ms) if ms else "-"
        sent_by  = log.get("userName", "Automatic")
        if cid not in client_info:
            client_info[cid] = {"name": ld.get("reportName", f"Client {cid}"),
                                 "time_val": time_val, "sent_by": sent_by, "has_send": True}
        else:
            client_info[cid].update({"time_val": time_val, "sent_by": sent_by, "has_send": True})

    return client_info


# ── SHARED: BUILD ROWS FROM DASHBOARD DATA ────────────────────────────────────
def build_rows_dashboard(input_date, client_map, status_map, send_log):
    rows = []
    for cid, name in client_map.items():
        if cid in EXCLUDED_IDS:
            continue
        rec           = status_map.get(cid)
        client_status = STATUS_MAP.get(rec["status"], rec["status"].capitalize()) if rec else "Unknown"
        report_raw    = rec.get("reportStatus", "notsent") if rec else "notsent"
        if client_status in ("Deactivated", "Unknown", "Hold"):
            continue
        if cid in send_log:
            entry         = send_log[cid]
            time_val      = entry["time"].split(" ")[1]
            sent_by       = entry.get("userName", "Automatic")
            report_status = "Sent"
        else:
            time_val, sent_by = "-", "-"
            report_status = "Sent" if report_raw == "sent" else "Pending"
        rows.append([input_date, name, cid, client_status, time_val, sent_by, report_status])
    return rows


# ── SHARED: HTML BUILDERS ─────────────────────────────────────────────────────
TABLE_HEADER = """
<tr style="background-color:#f2f2f2;">
  <th>Date</th><th>Client Name</th><th>Client ID</th><th>Client Status</th>
  <th>Report Sent Time</th><th>Sent By</th><th>Report Status</th>
</tr>
"""

def build_table(title, row_list):
    out  = f"<h4>{title}</h4>"
    out += '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">'
    out += TABLE_HEADER
    for r in row_list:
        s, rs, t = r[3], r[6], r[4]
        if s in ("Deactivated", "Hold"):      color = "#d1ecf1"
        elif rs == "Pending":                 color = "#f8d7da"
        elif t >= "08:00:00":                 color = "#ffe5b4"
        else:                                 color = "#d4edda"
        out += f"<tr style='background-color:{color};'>"
        for cell in r:
            out += f"<td>{cell}</td>"
        out += "</tr>\n"
    out += "</table>"
    return out


def sort_key(r):
    s, rs, t = r[3], r[6], r[4]
    if s in ("Deactivated", "Hold"):     return (4,)
    if rs == "Pending":                  return (1,)
    if rs == "Sent" and t >= "08:00:00": return (2, t)
    if rs == "Sent":                     return (3, t)
    return (5,)


# ── MORNING REPORT TASK ───────────────────────────────────────────────────────
def run_morning_report():
    yesterday      = datetime.now(ist) - timedelta(days=1)
    input_date     = yesterday.strftime("%d-%m-%Y")
    input_date_iso = yesterday.strftime("%Y-%m-%d")
    print(f"\n[Morning] Fetching report for: {input_date}")

    token_expired = False
    try:
        client_map, status_map, send_log = fetch_dashboard_all(input_date, input_date_iso)
        rows = build_rows_dashboard(input_date, client_map, status_map, send_log)
        data_source = "Dashboard"
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            print("  Token expired — OpenSearch fallback")
            token_expired = True
            _send_token_alert(input_date, "report_mail.py")
            client_info = fetch_opensearch_all(input_date)
            rows = []
            for cid, info in client_info.items():
                rs = "Sent" if info["has_send"] else "Pending"
                rows.append([input_date, info["name"], cid, "Active",
                             info["time_val"], info["sent_by"], rs])
            data_source = "OpenSearch"
        else:
            print(f"  HTTP error: {e}")
            return

    rows.sort(key=sort_key)
    priority_rows = [r for r in rows if r[2] in PRIORITY_IDS]
    other_rows    = [r for r in rows if r[2] not in PRIORITY_IDS]

    total   = len(rows)
    sent    = sum(1 for r in rows if r[6] == "Sent")
    pending = sum(1 for r in rows if r[6] == "Pending")
    deact   = sum(1 for r in rows if r[3] in ("Deactivated", "Hold"))

    source_note = (
        '<p style="color:#e67e22;font-weight:bold;">⚠ Data sourced from OpenSearch — '
        'dashboard token expired. Check your email for update instructions.</p>'
    ) if token_expired else ""

    html = f"""
<h3>Report Status - {input_date}</h3>
{source_note}
<h4>Summary</h4>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;text-align:center;">
<tr style="background-color:#f2f2f2;">
  <th>Total Reports</th><th>Deactivated + Hold</th><th>Sent Reports</th><th>Pending (Active Only)</th>
</tr>
<tr>
  <td>{total}</td>
  <td style="background-color:#e2e3e5;">{deact}</td>
  <td style="background-color:#d4edda;">{sent}</td>
  <td style="background-color:#f8d7da;">{pending}</td>
</tr>
</table><br><br>
"""
    html += build_table("Key Clients", priority_rows)
    html += "<br><br>"
    html += build_table("Other Clients", other_rows)

    csv_bytes = rows_to_csv_bytes(rows)
    send_brevo(f"Report Status - {input_date}", html, RECEIVER_EMAIL,
               csv_bytes, f"Report_Timing_{input_date}.csv")
    print(f"  [Morning] Mail sent. Source: {data_source}")


# ── MIDDAY CHECK TASK ─────────────────────────────────────────────────────────
def run_midday_check(input_date, input_date_iso):
    print(f"\n[Midday] Checking Go Colors for: {input_date}")

    token_expired = False
    try:
        auth  = {"Authorization": f"Bearer {API_TOKEN}"}
        jsonh = {**auth, "Content-Type": "application/json"}

        r = requests.get(f"{API_BASE}/client/get-clients", headers=auth, timeout=30)
        r.raise_for_status()
        all_clients = r.json()["data"]["result"]

        go_colors_id = next(
            (str(c["clientId"]) for c in all_clients
             if c["clientName"].lower() == GO_COLORS_LOWER),
            None,
        )
        print(f"  Go Colors ID: {go_colors_id}")

        all_ids_str = [str(c["clientId"]) for c in all_clients]
        client_map  = {str(c["clientId"]): c["clientName"] for c in all_clients}

        # Check Go Colors status
        r2 = requests.post(
            f"{API_BASE}/report/client-list-table", headers=jsonh,
            json={"fromDate": input_date_iso, "toDate": input_date_iso,
                  "clientId": [go_colors_id] if go_colors_id else [],
                  "reportStatus": "", "sortBy": -1, "limit": 10, "offset": 1},
            timeout=30,
        )
        r2.raise_for_status()
        go_sent = False
        if r2.status_code != 204 and r2.content:
            for rec in r2.json().get("data", {}).get("result", []):
                if str(rec.get("tangoId")) == go_colors_id and rec.get("reportStatus") == "sent":
                    go_sent = True

        if not go_sent:
            print("  Go Colors not yet sent — skipping.")
            return

        print("  Go Colors confirmed sent — fetching all clients.")

        # Fetch all clients for full report
        client_map, status_map, send_log = fetch_dashboard_all(input_date, input_date_iso)
        rows = build_rows_dashboard(input_date, client_map, status_map, send_log)
        data_source = "Dashboard"

    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            print("  Token expired — OpenSearch fallback")
            token_expired = True
            _send_token_alert(input_date, "report_mail_midday.py")
            client_info = fetch_opensearch_all(input_date)

            go_sent = any(
                info["has_send"] and name == GO_COLORS_LOWER
                for name, info in (
                    (client_info[cid]["name"].lower(), client_info[cid])
                    for cid in client_info
                )
            )
            if not go_sent:
                print("  Go Colors not yet sent (OpenSearch) — skipping.")
                return

            rows = []
            for cid, info in client_info.items():
                rs = "Sent" if info["has_send"] else "Pending"
                rows.append([input_date, info["name"], cid, "Active",
                             info["time_val"], info["sent_by"], rs])
            data_source = "OpenSearch"
        else:
            print(f"  HTTP error: {e}")
            return

    # Mark as sent (prevent duplicate if service restarts mid-day)
    with _midday_lock:
        _midday_sent.add(input_date)

    rows.sort(key=sort_key)
    key_rows   = [r for r in rows if r[1].lower() in KEY_NAMES_LOWER]
    other_rows = [r for r in rows if r[1].lower() not in KEY_NAMES_LOWER]

    total   = len(rows)
    sent    = sum(1 for r in rows if r[6] == "Sent")
    pending = sum(1 for r in rows if r[6] == "Pending")
    triggered_at = datetime.now(ist).strftime("%I:%M %p IST")

    source_note = (
        '<p style="color:#e67e22;font-weight:bold;">⚠ Data sourced from OpenSearch — '
        'dashboard token expired. Check your email for update instructions.</p>'
    ) if token_expired else ""

    html = f"""
<h3>Midday Report Check - {input_date}</h3>
<p style="color:#555;">Triggered at {triggered_at} after Go Colors confirmed sent.</p>
{source_note}
<h4>Summary</h4>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;text-align:center;">
<tr style="background-color:#f2f2f2;">
  <th>Total Reports</th><th>Sent Reports</th><th>Pending (Active Only)</th>
</tr>
<tr>
  <td>{total}</td>
  <td style="background-color:#d4edda;">{sent}</td>
  <td style="background-color:#f8d7da;">{pending}</td>
</tr>
</table><br><br>
"""
    html += build_table("Key Clients", key_rows)
    html += "<br><br>"
    html += build_table("Other Clients", other_rows)

    csv_bytes = rows_to_csv_bytes(rows)
    send_brevo(f"Midday Report Check - {input_date}", html, RECEIVER_EMAIL,
               csv_bytes, f"Midday_Report_{input_date}.csv")
    print(f"  [Midday] Mail sent. Source: {data_source}")


def _send_token_alert(input_date, filename):
    html = f"""
    <h3 style="color:#c0392b;">Dashboard Token Expired</h3>
    <p>Report for <b>{input_date}</b> was generated using <b>OpenSearch</b> as fallback.</p>
    <p>To restore: open the GitHub repo → edit <code>{filename}</code> → update the token default value → commit.</p>
    """
    send_brevo(f"ACTION REQUIRED: Dashboard Token Expired – {input_date}", html, ALERT_EMAIL)
    print(f"  Token alert sent to {ALERT_EMAIL}")


# ── FLASK ROUTES ──────────────────────────────────────────────────────────────
@app.route("/")
def health():
    now = datetime.now(ist).strftime("%d-%m-%Y %H:%M IST")
    return jsonify({"status": "ok", "time": now}), 200


@app.route("/tasks/morning-report")
def morning_report():
    if request.args.get("key") != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    thread = threading.Thread(target=run_morning_report, daemon=True)
    thread.start()
    return jsonify({"status": "started", "task": "morning-report"}), 200


@app.route("/tasks/midday-check")
def midday_check():
    if request.args.get("key") != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    yesterday      = datetime.now(ist) - timedelta(days=1)
    input_date     = yesterday.strftime("%d-%m-%Y")
    input_date_iso = yesterday.strftime("%Y-%m-%d")

    with _midday_lock:
        if input_date in _midday_sent:
            return jsonify({"status": "already_sent", "date": input_date}), 200

    thread = threading.Thread(
        target=run_midday_check, args=(input_date, input_date_iso), daemon=True
    )
    thread.start()
    return jsonify({"status": "checking", "date": input_date}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
