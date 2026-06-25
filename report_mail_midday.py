import requests
import time
from datetime import datetime, timezone, timedelta
import csv
import base64
import os

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_TOKEN      = os.environ.get("DASHBOARD_API_TOKEN", "9f0a75c6-097a-422c-883c-5946fb2d0d2f")
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

API_BASE    = "https://dashboard-api.tangoeye.ai/v3"
AUTH_HEADER = {"Authorization": f"Bearer {API_TOKEN}"}
JSON_HEADER = {**AUTH_HEADER, "Content-Type": "application/json"}

ist            = timezone(timedelta(hours=5, minutes=30))
yesterday      = datetime.now(ist) - timedelta(days=1)
input_date     = yesterday.strftime("%d-%m-%Y")
input_date_iso = yesterday.strftime("%Y-%m-%d")

MAX_RETRIES  = 6   # retry every hour up to 6 PM IST
GO_COLORS_LOWER = "go colors"

EXCLUDED_IDS = {"201", "425", "458", "468", "520"}

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

print(f"[Midday] Checking report for: {input_date}")


# ── BREVO ─────────────────────────────────────────────────────────────────────
def send_brevo(subject, html_body, to_csv, attachment_path=None):
    to_list = [{"email": e.strip()} for e in to_csv.split(",") if e.strip()]
    payload = {
        "sender":      {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to":          to_list,
        "subject":     subject,
        "htmlContent": html_body,
    }
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            payload["attachment"] = [{"content": base64.b64encode(f.read()).decode(),
                                      "name":    os.path.basename(attachment_path)}]
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    resp.raise_for_status()


# ── TOKEN ALERT ───────────────────────────────────────────────────────────────
def send_token_alert():
    html = f"""
    <h3 style="color:#c0392b;">Dashboard Token Expired</h3>
    <p>The midday report for <b>{input_date}</b> was generated using <b>OpenSearch</b>.</p>
    <p>Open GitHub repo → edit <code>report_mail_midday.py</code> → update the token → commit.</p>
    """
    send_brevo(f"ACTION REQUIRED: Dashboard Token Expired – {input_date}", html, ALERT_EMAIL)


# ── DASHBOARD ─────────────────────────────────────────────────────────────────
def fetch_from_dashboard():
    r = requests.get(f"{API_BASE}/client/get-clients", headers=AUTH_HEADER, timeout=30)
    r.raise_for_status()
    all_clients = r.json()["data"]["result"]
    all_ids_str = [str(c["clientId"]) for c in all_clients]
    client_map  = {str(c["clientId"]): c["clientName"] for c in all_clients}

    go_colors_id = next(
        (str(c["clientId"]) for c in all_clients
         if c["clientName"].lower() == GO_COLORS_LOWER), None,
    )
    print(f"  Clients: {len(all_clients)}  Go Colors ID: {go_colors_id}")

    status_map = {}
    offset, limit = 1, 500
    while True:
        r2 = requests.post(
            f"{API_BASE}/report/client-list-table", headers=JSON_HEADER,
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

    # Gate: Go Colors must be sent
    if go_colors_id:
        go_rec    = status_map.get(go_colors_id)
        go_status = go_rec.get("reportStatus", "notsent") if go_rec else "notsent"
        if go_status != "sent":
            print("  Go Colors not yet sent.")
            return None, None
        print("  Go Colors confirmed sent — proceeding.")
    else:
        print("  Go Colors not found.")
        return None, None

    sent_ids = [cid for cid, rec in status_map.items() if rec.get("reportStatus") == "sent"]
    send_log  = {}
    for cid in sent_ids:
        try:
            r3 = requests.post(
                f"{API_BASE}/report/get-report-log", headers=JSON_HEADER,
                json={"clientId": cid, "fileDate": input_date}, timeout=30,
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
    return rows, "Dashboard"


# ── OPENSEARCH FALLBACK ───────────────────────────────────────────────────────
def fetch_from_opensearch():
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

    client_info    = {}
    go_colors_sent = False

    for log in generate_logs:
        ld  = log.get("logData", {})
        cid = str(ld.get("clientId", ""))
        if not cid or cid in EXCLUDED_IDS:
            continue
        if cid not in client_info:
            client_info[cid] = {"name": ld.get("reportName", f"Client {cid}"),
                                 "time_val": "-", "sent_by": "-", "has_send": False}

    for log in send_logs:
        ld   = log.get("logData", {})
        cid  = str(ld.get("clientId", ""))
        name = ld.get("reportName", "")
        if not cid or cid in EXCLUDED_IDS:
            continue
        if name.lower() == GO_COLORS_LOWER:
            go_colors_sent = True
        if cid in client_info and client_info[cid]["has_send"]:
            continue
        ms       = log.get("createdAt", 0)
        time_val = epoch_to_ist(ms) if ms else "-"
        sent_by  = log.get("userName", "Automatic")
        if cid not in client_info:
            client_info[cid] = {"name": name, "time_val": time_val,
                                 "sent_by": sent_by, "has_send": True}
        else:
            client_info[cid].update({"time_val": time_val, "sent_by": sent_by, "has_send": True})

    if not go_colors_sent:
        print("  Go Colors not yet sent (OpenSearch).")
        return None, None
    print("  Go Colors confirmed sent (OpenSearch).")

    rows = []
    for cid, info in client_info.items():
        rs = "Sent" if info["has_send"] else "Pending"
        rows.append([input_date, info["name"], cid, "Active",
                     info["time_val"], info["sent_by"], rs])
    return rows, "OpenSearch"


# ── RETRY LOOP ────────────────────────────────────────────────────────────────
token_alert_sent = False
rows = None
data_source = None
token_expired = False

for attempt in range(MAX_RETRIES + 1):
    now_ist = datetime.now(ist)
    print(f"\n[Attempt {attempt + 1}/{MAX_RETRIES + 1}] {now_ist.strftime('%H:%M IST')}")

    token_expired = False
    try:
        rows, data_source = fetch_from_dashboard()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            print("  Token expired — OpenSearch fallback")
            token_expired = True
            if not token_alert_sent:
                send_token_alert()
                token_alert_sent = True
            rows, data_source = fetch_from_opensearch()
        else:
            raise

    if rows is not None:
        break

    if attempt < MAX_RETRIES:
        next_check = now_ist + timedelta(hours=1)
        print(f"  Sleeping 1 hour. Next check: {next_check.strftime('%H:%M IST')}")
        time.sleep(3600)
    else:
        print("Go Colors not sent by 6:00 PM IST. No midday email today.")
        raise SystemExit(0)

# ── SORT & SPLIT ──────────────────────────────────────────────────────────────
def sort_key(r):
    rs, t = r[6], r[4]
    if rs == "Pending":                  return (1,)
    if rs == "Sent" and t >= "08:00:00": return (2, t)
    if rs == "Sent":                     return (3, t)
    return (4,)

rows.sort(key=sort_key)
key_rows   = [r for r in rows if r[1].lower() in KEY_NAMES_LOWER]
other_rows = [r for r in rows if r[1].lower() not in KEY_NAMES_LOWER]

# ── CSV ───────────────────────────────────────────────────────────────────────
file_name = f"Midday_Report_{input_date}.csv"
with open(file_name, mode="w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["Date", "Client Name", "Client ID", "Client Status",
                     "Report Sent Time", "Sent By", "Report Status"])
    writer.writerows(rows)

# ── COUNTS ────────────────────────────────────────────────────────────────────
last_checked = datetime.now(ist).strftime("%I:%M %p")
total       = len(rows)
sent        = sum(1 for r in rows if r[6] == "Sent")
pending     = sum(1 for r in rows if r[6] == "Pending")
auto_sent   = sum(1 for r in rows if r[6] == "Sent" and r[5] == "Automatic")
manual_sent = sum(1 for r in rows if r[6] == "Sent" and r[5] not in ("Automatic", "-"))

source_note = (
    '<p style="color:#e67e22;font-weight:bold;">⚠ Data sourced from OpenSearch — '
    'dashboard token expired. Check your email for update instructions.</p>'
) if token_expired else ""

# ── HTML ──────────────────────────────────────────────────────────────────────
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

html = f"""
<h3>Report Status - {input_date}</h3>
<p style="color:#555; font-size:13px;"><b>Last checked: {last_checked}</b></p>
{source_note}
<h4>Summary</h4>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse; text-align:center;">
<tr style="background-color:#f2f2f2;">
  <th>Total Reports</th><th>Sent Reports</th><th>Pending (Active Only)</th>
  <th>Auto Sent Count</th><th>Manually Sent Count</th>
</tr>
<tr>
  <td>{total}</td>
  <td style="background-color:#d4edda;">{sent}</td>
  <td style="background-color:#f8d7da;">{pending}</td>
  <td style="background-color:#cce5ff;">{auto_sent}</td>
  <td style="background-color:#fff3cd;">{manual_sent}</td>
</tr>
</table><br><br>
"""
html += build_table("Key Clients", key_rows)
html += "<br><br>"
html += build_table("Other Clients", other_rows)

# ── SEND ──────────────────────────────────────────────────────────────────────
send_brevo(f"Report Status - {input_date}", html, RECEIVER_EMAIL, file_name)
print(f"Midday mail sent. Source: {data_source}  Last checked: {last_checked}")
