import io
import csv
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, Response

# ── CONFIG ────────────────────────────────────────────────────────────────────
import os
API_TOKEN = os.environ.get("DASHBOARD_API_TOKEN", "912347ba-72eb-48f2-9e70-23301d2f1eb4")
API_BASE  = "https://dashboard-api.tangoeye.ai/v3"

EXCLUDED_IDS  = {"201", "425", "458", "468", "520"}
PRIORITY_IDS  = {"193", "455", "185", "460", "94", "469", "95", "409", "4", "11", "322", "434"}
STATUS_MAP    = {"active": "Active", "deactive": "Deactivated", "suspended": "Hold"}

app = Flask(__name__)

# ── HELPERS ───────────────────────────────────────────────────────────────────
def auth_headers():
    return {"Authorization": f"Bearer {API_TOKEN}"}

def json_headers():
    return {**auth_headers(), "Content-Type": "application/json"}

def fetch_report_data(date_str: str) -> list[list]:
    """
    date_str: DD-MM-YYYY  e.g. "24-06-2026"
    Returns rows: [date, client_name, client_id, client_status, sent_time, sent_by, report_status]
    """
    date_iso = datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y-%m-%d")

    # Step 1 – all clients
    r = requests.get(f"{API_BASE}/client/get-clients", headers=auth_headers(), timeout=30)
    r.raise_for_status()
    all_clients = r.json()["data"]["result"]
    all_ids_str = [str(c["clientId"]) for c in all_clients]
    client_map  = {str(c["clientId"]): c["clientName"] for c in all_clients}

    # Step 2 – report status (paginated)
    status_map, offset, limit = {}, 1, 500
    while True:
        r2 = requests.post(
            f"{API_BASE}/report/client-list-table",
            headers=json_headers(),
            json={
                "fromDate": date_iso, "toDate": date_iso,
                "clientId": all_ids_str, "reportStatus": "",
                "sortBy": -1, "limit": limit, "offset": offset,
            },
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

    # Step 3 – send logs for "sent" clients
    sent_ids = [cid for cid, rec in status_map.items() if rec.get("reportStatus") == "sent"]
    send_log = {}
    for cid in sent_ids:
        try:
            r3 = requests.post(
                f"{API_BASE}/report/get-report-log",
                headers=json_headers(),
                json={"clientId": cid, "fileDate": date_str},
                timeout=30,
            )
            r3.raise_for_status()
            for log in r3.json().get("data", {}).get("result", []):
                if log.get("logSubType") != "sendReport":
                    continue
                if log.get("logData", {}).get("fileDate") != date_str:
                    continue
                prev = send_log.get(cid)
                if prev is None or log["createdAt"] > prev["createdAt"]:
                    send_log[cid] = log
        except Exception:
            pass

    # Step 4 – build rows
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
            time_val = "-"
            sent_by  = "-"
            report_status = "Sent" if report_raw == "sent" else ("Pending" if client_status == "Active" else "-")

        priority = "Yes" if cid in PRIORITY_IDS else "No"
        rows.append([date_str, name, cid, client_status, time_val, sent_by, report_status, priority])

    # Sort: Pending first, then late sent, then on-time sent
    def sort_key(r):
        s, rs, t = r[3], r[6], r[4]
        if rs == "Pending":                  return (1,)
        if rs == "Sent" and t >= "08:00:00": return (2, t)
        if rs == "Sent":                     return (3, t)
        return (4,)

    rows.sort(key=sort_key)
    return rows


# ── ENDPOINT ──────────────────────────────────────────────────────────────────
@app.route("/report/export-csv", methods=["GET"])
def export_csv():
    """
    GET /report/export-csv?date=24-06-2026
    date is optional — defaults to yesterday (IST).
    Returns a CSV file download.
    """
    date_param = request.args.get("date")

    if date_param:
        try:
            datetime.strptime(date_param, "%d-%m-%Y")
        except ValueError:
            return {"error": "date must be DD-MM-YYYY"}, 400
        date_str = date_param
    else:
        ist      = timezone(timedelta(hours=5, minutes=30))
        date_str = (datetime.now(ist) - timedelta(days=1)).strftime("%d-%m-%Y")

    try:
        rows = fetch_report_data(date_str)
    except requests.HTTPError as e:
        return {"error": f"Upstream API error: {e.response.status_code}"}, 502
    except Exception as e:
        return {"error": str(e)}, 500

    # Build CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Client Name", "Client ID", "Client Status",
                     "Report Sent Time", "Sent By", "Report Status", "Priority Client"])
    writer.writerows(rows)

    filename = f"Report_Timing_{date_str}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
