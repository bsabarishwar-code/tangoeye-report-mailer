from flask import Flask, request, jsonify, Response
import threading
import requests
from datetime import datetime, timezone, timedelta
import csv
import base64
import os
import io

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
CRON_SECRET        = os.environ.get("CRON_SECRET",         "changeme")
NEON_DATABASE_URL  = os.environ.get("NEON_DATABASE_URL",  "")
API_TOKEN          = os.environ.get("DASHBOARD_API_TOKEN", "9f0a75c6-097a-422c-883c-5946fb2d0d2f")
BREVO_API_KEY  = os.environ.get("BREVO_API_KEY",       "")
SENDER_EMAIL   = os.environ.get("SENDER_EMAIL",        "tangoeye.ops@gmail.com")
SENDER_NAME    = os.environ.get("SENDER_NAME",         "TangoEye Reports")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAILS",
    "sabarishwar@tangotech.co.in,ravichandran@tangotech.co.in,lokesh@tangotech.co.in,"
    "Hariharan@tangotech.co.in,Prasannavenkatesh@tangotech.co.in")
ALERT_EMAIL    = "sabarishwar@tangotech.co.in"

OS_HOST  = "search-tango-prod-7dsufau4cxzttx6yt7dqc3rzme.ap-south-1.es.amazonaws.com"
OS_USER  = os.environ.get("OPENSEARCH_USER", "ravi")
OS_PASS  = os.environ.get("OPENSEARCH_PASS", "T@ng0#2024")
OS_INDEX = "tango-audit-activity-logs"

API_BASE = "https://dashboard-api.tangoeye.ai/v3"
ist      = timezone(timedelta(hours=5, minutes=30))

EXCLUDED_IDS    = {"201", "425", "458", "468", "520"}
GO_COLORS_LOWER = "go colors"

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

STATUS_MAP = {"active": "Active", "deactive": "Deactivated", "suspended": "Hold"}

# In-memory threading state (fallback when DB is unavailable)
_lock            = threading.Lock()
_midday_sent     = set()
_early_msg_ids   = {}
_morning_msg_ids = {}


# ── NEON DB (thread ID persistence across Render restarts) ────────────────────
def _db_conn():
    import psycopg2
    return psycopg2.connect(NEON_DATABASE_URL, sslmode="require")

def init_db():
    if not NEON_DATABASE_URL:
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS report_thread_ids (
                        date TEXT PRIMARY KEY,
                        early_msg_id TEXT,
                        morning_msg_id TEXT
                    )
                """)
        print("  DB ready")
    except Exception as e:
        print(f"  DB init error: {e}")

def db_save_msg_id(date, slot, msg_id):
    if not NEON_DATABASE_URL or not msg_id:
        return
    if slot not in ("early_msg_id", "morning_msg_id"):
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO report_thread_ids (date, {slot}) VALUES (%s, %s) "
                    f"ON CONFLICT (date) DO UPDATE SET {slot} = EXCLUDED.{slot}",
                    (date, msg_id),
                )
        print(f"  DB saved {slot} for {date}")
    except Exception as e:
        print(f"  DB save error: {e}")

def db_get_early_msg_id(date):
    if not NEON_DATABASE_URL:
        return None
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT early_msg_id FROM report_thread_ids WHERE date = %s", (date,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        print(f"  DB read error: {e}")
        return None

def db_get_reply_to(date):
    """Returns morning_msg_id if present, else early_msg_id — for midday threading."""
    if not NEON_DATABASE_URL:
        return None
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT early_msg_id, morning_msg_id FROM report_thread_ids WHERE date = %s",
                    (date,),
                )
                row = cur.fetchone()
                if row:
                    return row[1] or row[0]
        return None
    except Exception as e:
        print(f"  DB read error: {e}")
        return None


init_db()


# ── BREVO ─────────────────────────────────────────────────────────────────────
def send_brevo(subject, html_body, to_csv, csv_bytes=None, csv_filename=None,
               reply_to_msg_id=None):
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
    if reply_to_msg_id:
        payload["headers"] = {
            "In-Reply-To": reply_to_msg_id,
            "References":  reply_to_msg_id,
        }
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("messageId")


def rows_to_csv_bytes(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Client Name", "Client ID", "Client Status",
                     "Report Sent Time", "Sent By", "Report Status"])
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def send_token_alert(input_date, script_name):
    html = f"""
    <h3 style="color:#c0392b;">Dashboard Token Expired</h3>
    <p>Report for <b>{input_date}</b> was generated using <b>OpenSearch</b> as fallback.</p>
    <p>Open GitHub repo → edit <code>{script_name}</code> → update the token → commit.</p>
    """
    send_brevo(f"ACTION REQUIRED: Dashboard Token Expired – {input_date}", html, ALERT_EMAIL)
    print(f"  Token alert sent to {ALERT_EMAIL}")


# ── DASHBOARD FETCH (all clients) ─────────────────────────────────────────────
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
    manually_generated_cids = set()
    for cid in sent_ids:
        try:
            r3 = requests.post(
                f"{API_BASE}/report/get-report-log", headers=jsonh,
                json={"clientId": cid, "fileDate": input_date}, timeout=30,
            )
            r3.raise_for_status()
            for log in r3.json().get("data", {}).get("result", []):
                if log.get("logData", {}).get("fileDate") != input_date:
                    continue
                sub = log.get("logSubType")
                if sub == "sendReport":
                    prev = send_log.get(cid)
                    if prev is None or log["createdAt"] > prev["createdAt"]:
                        send_log[cid] = log
                elif sub == "generateReport":
                    uname = log.get("userName", "Automatic")
                    if uname and uname != "Automatic":
                        manually_generated_cids.add(cid)
        except Exception as e:
            print(f"  Warning: log fetch failed for {cid}: {e}")
    print(f"  Send logs: {len(send_log)}  Manually generated: {len(manually_generated_cids)}")
    return client_map, status_map, send_log, manually_generated_cids


# ── OPENSEARCH FETCH (all clients) ────────────────────────────────────────────
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

    client_info = {}
    for log in generate_logs:
        ld  = log.get("logData", {})
        cid = str(ld.get("clientId", ""))
        if not cid or cid in EXCLUDED_IDS:
            continue
        uname   = log.get("userName", "Automatic")
        man_gen = bool(uname and uname != "Automatic")
        if cid not in client_info:
            client_info[cid] = {"name": ld.get("reportName", f"Client {cid}"),
                                 "time_val": "-", "sent_by": "-", "has_send": False,
                                 "manually_generated": man_gen}
        elif man_gen:
            client_info[cid]["manually_generated"] = True

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
                                 "time_val": time_val, "sent_by": sent_by, "has_send": True,
                                 "manually_generated": False}
        else:
            client_info[cid].update({"time_val": time_val, "sent_by": sent_by, "has_send": True})
    return client_info


# ── BUILD ROWS FROM DASHBOARD DATA ────────────────────────────────────────────
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


# ── HTML BUILDERS ─────────────────────────────────────────────────────────────
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
        rs, t = r[6], r[4]
        if rs == "Pending":                 color = "#f8d7da"
        elif t >= "08:00:00":               color = "#ffe5b4"
        else:                               color = "#d4edda"
        out += f"<tr style='background-color:{color};'>"
        for cell in r:
            out += f"<td>{cell}</td>"
        out += "</tr>\n"
    out += "</table>"
    return out


def sort_key(r):
    rs, t = r[6], r[4]
    if rs == "Pending":                  return (1,)
    if rs == "Sent" and t >= "08:00:00": return (2, t)
    if rs == "Sent":                     return (3, t)
    return (4,)


def build_summary_html(rows, input_date, last_checked, token_expired, note="", manually_generated=0):
    total       = len(rows)
    sent        = sum(1 for r in rows if r[6] == "Sent")
    pending     = sum(1 for r in rows if r[6] == "Pending")
    auto_sent   = sum(1 for r in rows if r[6] == "Sent" and r[5] == "Automatic")
    manual_sent = sum(1 for r in rows if r[6] == "Sent" and r[5] not in ("Automatic", "-"))

    source_note = (
        '<p style="color:#e67e22;font-weight:bold;">⚠ Data sourced from OpenSearch — '
        'dashboard token expired. Check your email for update instructions.</p>'
    ) if token_expired else ""

    html = f"""
<h3>Report Status - {input_date}</h3>
<p style="color:#555; font-size:13px;"><b>Last checked: {last_checked}</b></p>
{note}
{source_note}
<h4>Summary</h4>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;text-align:center;">
<tr style="background-color:#f2f2f2;">
  <th>Total Reports</th><th>Sent Reports</th><th>Pending (Active Only)</th>
  <th>Auto Sent Count</th><th>Manually Sent Count</th><th>Manually Generated Count</th>
</tr>
<tr>
  <td>{total}</td>
  <td style="background-color:#d4edda;">{sent}</td>
  <td style="background-color:#f8d7da;">{pending}</td>
  <td style="background-color:#cce5ff;">{auto_sent}</td>
  <td style="background-color:#fff3cd;">{manual_sent}</td>
  <td style="background-color:#e8d5f5;">{manually_generated}</td>
</tr>
</table><br><br>
"""
    return html


# ── 6:30 AM REPORT TASK ───────────────────────────────────────────────────────
def run_early_report(recipients=None):
    recipients = recipients or RECEIVER_EMAIL
    yesterday      = datetime.now(ist) - timedelta(days=1)
    input_date     = yesterday.strftime("%d-%m-%Y")
    input_date_iso = yesterday.strftime("%Y-%m-%d")
    print(f"\n[Early] Fetching report for: {input_date}")

    token_expired     = False
    manually_generated = 0
    try:
        client_map, status_map, send_log, manual_gen_cids = fetch_dashboard_all(input_date, input_date_iso)
        rows = build_rows_dashboard(input_date, client_map, status_map, send_log)
        manually_generated = sum(1 for r in rows if r[2] in manual_gen_cids)
        data_source = "Dashboard"
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            print("  Token expired — OpenSearch fallback")
            token_expired = True
            send_token_alert(input_date, "report_server.py")
            client_info = fetch_opensearch_all(input_date)
            rows = [[input_date, i["name"], cid, "Active", i["time_val"], i["sent_by"],
                     "Sent" if i["has_send"] else "Pending"]
                    for cid, i in client_info.items()]
            manually_generated = sum(1 for i in client_info.values() if i.get("manually_generated"))
            data_source = "OpenSearch"
        else:
            print(f"  HTTP error: {e}")
            return

    rows.sort(key=sort_key)
    key_rows   = [r for r in rows if r[1].lower() in KEY_NAMES_LOWER]
    other_rows = [r for r in rows if r[1].lower() not in KEY_NAMES_LOWER]

    html  = build_summary_html(rows, input_date, "6:30 AM", token_expired,
                               manually_generated=manually_generated)
    html += build_table("Key Clients", key_rows)
    html += "<br><br>"
    html += build_table("Other Clients", other_rows)

    subject = f"Report Status - {input_date}"
    msg_id  = send_brevo(subject, html, recipients,
                         rows_to_csv_bytes(rows), f"Early_Report_{input_date}.csv")
    if msg_id:
        with _lock:
            _early_msg_ids[input_date] = msg_id
        db_save_msg_id(input_date, "early_msg_id", msg_id)
    print(f"  [Early] Mail sent. Source: {data_source}  msgId: {msg_id}")


# ── 9:30 AM REPORT TASK ───────────────────────────────────────────────────────
def run_morning_report(recipients=None):
    recipients = recipients or RECEIVER_EMAIL
    yesterday      = datetime.now(ist) - timedelta(days=1)
    input_date     = yesterday.strftime("%d-%m-%Y")
    input_date_iso = yesterday.strftime("%Y-%m-%d")
    print(f"\n[Morning] Fetching report for: {input_date}")

    token_expired     = False
    manually_generated = 0
    try:
        client_map, status_map, send_log, manual_gen_cids = fetch_dashboard_all(input_date, input_date_iso)
        rows = build_rows_dashboard(input_date, client_map, status_map, send_log)
        manually_generated = sum(1 for r in rows if r[2] in manual_gen_cids)
        data_source = "Dashboard"
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            print("  Token expired — OpenSearch fallback")
            token_expired = True
            send_token_alert(input_date, "report_server.py")
            client_info = fetch_opensearch_all(input_date)
            rows = [[input_date, i["name"], cid, "Active", i["time_val"], i["sent_by"],
                     "Sent" if i["has_send"] else "Pending"]
                    for cid, i in client_info.items()]
            manually_generated = sum(1 for i in client_info.values() if i.get("manually_generated"))
            data_source = "OpenSearch"
        else:
            print(f"  HTTP error: {e}")
            return

    rows.sort(key=sort_key)
    key_rows   = [r for r in rows if r[1].lower() in KEY_NAMES_LOWER]
    other_rows = [r for r in rows if r[1].lower() not in KEY_NAMES_LOWER]

    with _lock:
        reply_to = _early_msg_ids.get(input_date)
    if not reply_to:
        reply_to = db_get_early_msg_id(input_date)

    html  = build_summary_html(rows, input_date, "9:30 AM", token_expired,
                               manually_generated=manually_generated)
    html += build_table("Key Clients", key_rows)
    html += "<br><br>"
    html += build_table("Other Clients", other_rows)

    subject = f"Report Status - {input_date}"
    msg_id  = send_brevo(subject, html, recipients,
                         rows_to_csv_bytes(rows), f"Report_Timing_{input_date}.csv",
                         reply_to_msg_id=reply_to)
    if msg_id:
        with _lock:
            _morning_msg_ids[input_date] = msg_id
        db_save_msg_id(input_date, "morning_msg_id", msg_id)
    print(f"  [Morning] Mail sent. Source: {data_source}  msgId: {msg_id}")


# ── 12:00 PM MIDDAY CHECK TASK ────────────────────────────────────────────────
def run_midday_check(input_date, input_date_iso, recipients=None):
    recipients = recipients or RECEIVER_EMAIL
    print(f"\n[Midday] Checking for: {input_date}")
    token_expired    = False
    token_alert_done = False
    manually_generated = 0

    try:
        auth  = {"Authorization": f"Bearer {API_TOKEN}"}
        jsonh = {**auth, "Content-Type": "application/json"}

        r = requests.get(f"{API_BASE}/client/get-clients", headers=auth, timeout=30)
        r.raise_for_status()
        all_clients  = r.json()["data"]["result"]
        go_colors_id = next(
            (str(c["clientId"]) for c in all_clients
             if c["clientName"].lower() == GO_COLORS_LOWER), None,
        )
        print(f"  Go Colors ID: {go_colors_id}")

        # Quick Go Colors check
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
        client_map, status_map, send_log, manual_gen_cids = fetch_dashboard_all(input_date, input_date_iso)
        rows = build_rows_dashboard(input_date, client_map, status_map, send_log)
        manually_generated = sum(1 for r in rows if r[2] in manual_gen_cids)
        data_source = "Dashboard"

    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            print("  Token expired — OpenSearch fallback")
            token_expired = True
            if not token_alert_done:
                send_token_alert(input_date, "report_server.py")
                token_alert_done = True
            client_info = fetch_opensearch_all(input_date)
            go_sent = any(
                i["has_send"] and i["name"].lower() == GO_COLORS_LOWER
                for i in client_info.values()
            )
            if not go_sent:
                print("  Go Colors not yet sent (OpenSearch) — skipping.")
                return
            rows = [[input_date, i["name"], cid, "Active", i["time_val"], i["sent_by"],
                     "Sent" if i["has_send"] else "Pending"]
                    for cid, i in client_info.items()]
            manually_generated = sum(1 for i in client_info.values() if i.get("manually_generated"))
            data_source = "OpenSearch"
        else:
            print(f"  HTTP error: {e}")
            return

    with _lock:
        _midday_sent.add(input_date)
        reply_to = _morning_msg_ids.get(input_date) or _early_msg_ids.get(input_date)
    if not reply_to:
        reply_to = db_get_reply_to(input_date)

    rows.sort(key=sort_key)
    key_rows   = [r for r in rows if r[1].lower() in KEY_NAMES_LOWER]
    other_rows = [r for r in rows if r[1].lower() not in KEY_NAMES_LOWER]

    last_checked = datetime.now(ist).strftime("%I:%M %p")

    html  = build_summary_html(rows, input_date, last_checked, token_expired,
                               manually_generated=manually_generated)
    html += build_table("Key Clients", key_rows)
    html += "<br><br>"
    html += build_table("Other Clients", other_rows)

    subject = f"Report Status - {input_date}"
    send_brevo(subject, html, recipients,
               rows_to_csv_bytes(rows), f"Midday_Report_{input_date}.csv",
               reply_to_msg_id=reply_to)
    print(f"  [Midday] Mail sent. Source: {data_source}  Last checked: {last_checked}")


# ── FLASK ROUTES ──────────────────────────────────────────────────────────────
@app.route("/")
def health():
    now = datetime.now(ist).strftime("%d-%m-%Y %H:%M IST")
    return jsonify({"status": "ok", "time": now}), 200


@app.route("/tasks/early-report")
def early_report():
    if request.args.get("key") != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    threading.Thread(target=run_early_report, daemon=True).start()
    return jsonify({"status": "started", "task": "early-report"}), 200


@app.route("/tasks/morning-report")
def morning_report():
    if request.args.get("key") != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    threading.Thread(target=run_morning_report, daemon=True).start()
    return jsonify({"status": "started", "task": "morning-report"}), 200


@app.route("/tasks/midday-check")
def midday_check():
    if request.args.get("key") != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    yesterday      = datetime.now(ist) - timedelta(days=1)
    input_date     = yesterday.strftime("%d-%m-%Y")
    input_date_iso = yesterday.strftime("%Y-%m-%d")

    with _lock:
        if input_date in _midday_sent:
            return jsonify({"status": "already_sent", "date": input_date}), 200

    threading.Thread(target=run_midday_check,
                     args=(input_date, input_date_iso), daemon=True).start()
    return jsonify({"status": "checking", "date": input_date}), 200


@app.route("/tasks/test-report")
def test_report():
    if request.args.get("key") != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    threading.Thread(
        target=run_morning_report,
        kwargs={"recipients": "sabarishwar@tangotech.co.in"},
        daemon=True,
    ).start()
    return jsonify({"status": "started", "task": "test-report",
                    "recipients": "sabarishwar@tangotech.co.in"}), 200


def fetch_send_log_parallel(sent_ids, date_str, jsonh):
    """Fetch report logs for all sent clients in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(cid):
        try:
            r = requests.post(
                f"{API_BASE}/report/get-report-log", headers=jsonh,
                json={"clientId": cid, "fileDate": date_str}, timeout=20,
            )
            r.raise_for_status()
            best = None
            for log in r.json().get("data", {}).get("result", []):
                if log.get("logSubType") != "sendReport":
                    continue
                if log.get("logData", {}).get("fileDate") != date_str:
                    continue
                if best is None or log["createdAt"] > best["createdAt"]:
                    best = log
            return cid, best
        except Exception:
            return cid, None

    send_log = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_one, cid): cid for cid in sent_ids}
        for future in as_completed(futures):
            cid, entry = future.result()
            if entry:
                send_log[cid] = entry
    return send_log


@app.route("/report/export-csv", methods=["GET"])
def export_csv():
    """
    GET /report/export-csv?date=24-06-2026
    date is optional — defaults to yesterday (IST).
    Returns a CSV file download with all active clients' report timing.
    """
    date_param = request.args.get("date")
    if date_param:
        try:
            datetime.strptime(date_param, "%d-%m-%Y")
        except ValueError:
            return jsonify({"error": "date must be DD-MM-YYYY e.g. 24-06-2026"}), 400
        date_str = date_param
    else:
        date_str = (datetime.now(ist) - timedelta(days=1)).strftime("%d-%m-%Y")

    date_iso = datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y-%m-%d")

    try:
        auth  = {"Authorization": f"Bearer {API_TOKEN}"}
        jsonh = {**auth, "Content-Type": "application/json"}

        # Step 1 — all clients
        r = requests.get(f"{API_BASE}/client/get-clients", headers=auth, timeout=30)
        r.raise_for_status()
        all_clients = r.json()["data"]["result"]
        all_ids_str = [str(c["clientId"]) for c in all_clients]
        client_map  = {str(c["clientId"]): c["clientName"] for c in all_clients}

        # Step 2 — report status (paginated)
        status_map, offset, limit = {}, 1, 500
        while True:
            r2 = requests.post(
                f"{API_BASE}/report/client-list-table", headers=jsonh,
                json={"fromDate": date_iso, "toDate": date_iso,
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
            if not result or len(status_map) >= total:
                break
            offset += limit

        # Step 3 — logs in parallel
        sent_ids = [cid for cid, rec in status_map.items() if rec.get("reportStatus") == "sent"]
        send_log = fetch_send_log_parallel(sent_ids, date_str, jsonh)

    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        return jsonify({"error": f"Upstream API error: {code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    rows = build_rows_dashboard(date_str, client_map, status_map, send_log)
    rows.sort(key=sort_key)

    filename = f"Report_Timing_{date_str}.csv"
    return Response(
        rows_to_csv_bytes(rows),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
