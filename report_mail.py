import requests
from datetime import datetime, timezone, timedelta
import csv
import base64
import os

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_TOKEN      = os.environ.get("DASHBOARD_API_TOKEN", "9f0a75c6-097a-422c-883c-5946fb2d0d2f")
BREVO_API_KEY  = os.environ.get("BREVO_API_KEY",       "")
SENDER_EMAIL   = os.environ.get("SENDER_EMAIL",        "tangoeye.ops@gmail.com")
SENDER_NAME    = os.environ.get("SENDER_NAME",         "TangoEye Reports")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAILS",     "sabarishwar@tangotech.co.in,ravichandran@tangotech.co.in,lokesh@tangotech.co.in")
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
input_date     = yesterday.strftime("%d-%m-%Y")    # "23-06-2026"  → get-report-log / OpenSearch
input_date_iso = yesterday.strftime("%Y-%m-%d")    # "2026-06-23"  → client-list-table

EXCLUDED_IDS = {"201", "425", "458", "468", "520"}
PRIORITY_IDS = {"193", "455", "185", "460", "94", "469", "95", "409", "4", "11", "322", "434"}

STATUS_MAP = {
    "active":    "Active",
    "deactive":  "Deactivated",
    "suspended": "Hold",
}

print(f"Fetching report for: {input_date}")


# ── BREVO EMAIL HELPER ────────────────────────────────────────────────────────
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


# ── TOKEN EXPIRY ALERT ────────────────────────────────────────────────────────
def send_token_alert():
    html = f"""
    <h3 style="color:#c0392b;">Dashboard Token Expired</h3>
    <p>The report for <b>{input_date}</b> was generated using <b>OpenSearch</b> as a fallback.</p>
    <p>To restore normal operation, update the token:</p>
    <ol>
      <li>Log in to the dashboard → open Chrome DevTools → Network tab</li>
      <li>Make any API call → copy the <code>Authorization: Bearer &lt;token&gt;</code> value</li>
      <li>Open the GitHub repo → edit <code>Data/report_mail.py</code></li>
      <li>Replace the default value on line 8: <code>API_TOKEN = os.environ.get("DASHBOARD_API_TOKEN", "<b>PASTE-NEW-TOKEN-HERE</b>")</code></li>
      <li>Commit — Render will auto-redeploy and the next run will use the new token</li>
    </ol>
    """
    send_brevo(f"ACTION REQUIRED: Dashboard Token Expired – {input_date}", html, ALERT_EMAIL)
    print(f"  Token alert sent to {ALERT_EMAIL}")


# ── DASHBOARD DATA ────────────────────────────────────────────────────────────
def fetch_from_dashboard():
    # STEP 1: all clients
    r = requests.get(f"{API_BASE}/client/get-clients", headers=AUTH_HEADER, timeout=30)
    r.raise_for_status()
    all_clients = r.json()["data"]["result"]
    all_ids_str = [str(c["clientId"]) for c in all_clients]
    client_map  = {str(c["clientId"]): c["clientName"] for c in all_clients}
    print(f"  Clients: {len(all_clients)}")

    # STEP 2: report status (paginated)
    status_map = {}
    offset, limit = 1, 500
    while True:
        r2 = requests.post(
            f"{API_BASE}/report/client-list-table",
            headers=JSON_HEADER,
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

    # STEP 3: send logs for "sent" clients only
    sent_ids = [cid for cid, rec in status_map.items() if rec.get("reportStatus") == "sent"]
    send_log = {}
    for cid in sent_ids:
        try:
            r3 = requests.post(
                f"{API_BASE}/report/get-report-log",
                headers=JSON_HEADER,
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

    # Build rows
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
            if report_raw == "sent":
                report_status = "Sent"
            elif client_status == "Active":
                report_status = "Pending"
            else:
                report_status = "-"

        rows.append([input_date, name, cid, client_status, time_val, sent_by, report_status])
    return rows


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
                {"term": {"logType.keyword":          "report"}},
                {"term": {"logSubType.keyword":        subtype}},
                {"term": {"logData.fileDate.keyword":  input_date}},
            ]}},
            "sort": [{"createdAt": {"order": "desc"}}],
            "size": 2000,
        })
        return [h["_source"] for h in resp["hits"]["hits"]]

    def epoch_to_ist(epoch_ms):
        dt_ist = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc) + timedelta(hours=5, minutes=30)
        return dt_ist.strftime("%H:%M:%S")

    send_logs     = query_logs("sendReport")
    generate_logs = query_logs("generateReport")
    print(f"  OpenSearch sendReport: {len(send_logs)}  generateReport: {len(generate_logs)}")

    client_info = {}   # str clientId → dict

    # generateReport first (lower priority)
    for log in generate_logs:
        ld  = log.get("logData", {})
        cid = str(ld.get("clientId", ""))
        if not cid or cid in EXCLUDED_IDS:
            continue
        if cid not in client_info:
            client_info[cid] = {
                "name":     ld.get("reportName", f"Client {cid}"),
                "time_val": "-",
                "sent_by":  "-",
                "has_send": False,
            }

    # sendReport (higher priority, logs sorted desc → first hit = latest send)
    for log in send_logs:
        ld  = log.get("logData", {})
        cid = str(ld.get("clientId", ""))
        if not cid or cid in EXCLUDED_IDS:
            continue
        if cid in client_info and client_info[cid]["has_send"]:
            continue   # keep only the latest send per client
        epoch_ms = log.get("createdAt", 0)
        time_val = epoch_to_ist(epoch_ms) if epoch_ms else "-"
        sent_by  = log.get("userName", "Automatic")
        if cid not in client_info:
            client_info[cid] = {"name": ld.get("reportName", f"Client {cid}"),
                                 "time_val": time_val, "sent_by": sent_by, "has_send": True}
        else:
            client_info[cid].update({"time_val": time_val, "sent_by": sent_by, "has_send": True})

    rows = []
    for cid, info in client_info.items():
        report_status = "Sent" if info["has_send"] else "Pending"
        rows.append([input_date, info["name"], cid, "Active",
                     info["time_val"], info["sent_by"], report_status])
    return rows


# ── MAIN: dashboard first, OpenSearch if token expired ────────────────────────
token_expired = False
try:
    rows = fetch_from_dashboard()
    data_source = "Dashboard"
except requests.HTTPError as e:
    if e.response is not None and e.response.status_code in (401, 403):
        print("  Token expired — switching to OpenSearch fallback")
        token_expired = True
        send_token_alert()
        rows = fetch_from_opensearch()
        data_source = "OpenSearch"
    else:
        raise

print(f"  Rows: {len(rows)}  |  Source: {data_source}")

# ── SORT ──────────────────────────────────────────────────────────────────────
def sort_key(r):
    s, rs, t = r[3], r[6], r[4]
    if s in ("Deactivated", "Hold"):     return (4,)
    if rs == "Pending":                  return (1,)
    if rs == "Sent" and t >= "08:00:00": return (2, t)
    if rs == "Sent":                     return (3, t)
    return (5,)

rows.sort(key=sort_key)
priority_rows = [r for r in rows if r[2] in PRIORITY_IDS]
other_rows    = [r for r in rows if r[2] not in PRIORITY_IDS]

# ── SAVE CSV ──────────────────────────────────────────────────────────────────
file_name = f"Report_Timing_{input_date}.csv"
with open(file_name, mode="w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["Date", "Client Name", "Client ID", "Client Status",
                     "Report Sent Time", "Sent By", "Report Status"])
    writer.writerows(rows)
print(f"  CSV saved: {file_name}")

# ── SUMMARY ───────────────────────────────────────────────────────────────────
total_reports  = len(rows)
sent_reports   = sum(1 for r in rows if r[6] == "Sent")
pending_active = sum(1 for r in rows if r[6] == "Pending")
deact_hold     = sum(1 for r in rows if r[3] in ("Deactivated", "Hold"))

source_note = (
    '<p style="color:#e67e22;font-weight:bold;">'
    '⚠ Data sourced from OpenSearch — dashboard token expired. '
    'Check your email for update instructions.</p>'
) if token_expired else ""

# ── HTML ──────────────────────────────────────────────────────────────────────
html = f"""
<h3>Report Status - {input_date}</h3>
{source_note}
<h4>Summary</h4>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse; text-align:center;">
<tr style="background-color:#f2f2f2;">
  <th>Total Reports</th><th>Deactivated + Hold</th><th>Sent Reports</th><th>Pending (Active Only)</th>
</tr>
<tr>
  <td>{total_reports}</td>
  <td style="background-color:#e2e3e5;">{deact_hold}</td>
  <td style="background-color:#d4edda;">{sent_reports}</td>
  <td style="background-color:#f8d7da;">{pending_active}</td>
</tr>
</table>
<br><br>
"""

TABLE_HEADER = """
<tr style="background-color:#f2f2f2;">
  <th>Date</th><th>Client Name</th><th>Client ID</th><th>Client Status</th>
  <th>Report Sent Time</th><th>Sent By</th><th>Report Status</th>
</tr>
"""

def build_table(title, row_list):
    out = f"<h4>{title}</h4>"
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

html += build_table("Key Clients", priority_rows)
html += "<br><br>"
html += build_table("Other Clients", other_rows)

# ── SEND ──────────────────────────────────────────────────────────────────────
send_brevo(f"Report Status - {input_date}", html, RECEIVER_EMAIL, file_name)
print("Mail sent successfully via Brevo")
