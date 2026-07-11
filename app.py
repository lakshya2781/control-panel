from flask import Flask, jsonify
import requests
import datetime, os
from datetime import timezone, timedelta
import psycopg2

app = Flask(__name__)
 
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

DATABASE_URL = os.environ.get("DATABASE_URL")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO")

# --- THRESHOLDS ---
POPULATION_DECREASE_LIMIT = -180   # alert if any state drops by 180+ in one update
POPULATION_INCREASE_LIMIT = 450    # alert if any state rises by 450+ in one update
CALLS_MULTIPLE = 1000              # alert every time total calls crosses another 1000
SMS_MULTIPLE = 4000                # alert every time total sms crosses another 4000
STOCK_TICK_SPIKE_PCT = 3.0         # alert if any stock moves 3%+ in a single tick
# STOCK_DAY_CHANGE_LIMIT_PCT = 200.0 # alert if any stock's day change crosses 200%
STOCK_PRICE_MILESTONE = 2000       # alert every time a stock price crosses another ₹2000 mark

# 👇 EDIT THESE WITH YOUR REAL LIVE URLS
services = [
    {"name": "📡 Telecom Observability", "url": "https://telecom-observer.onrender.com"},
    {"name": "🔢 Counter Server", "url": "https://counter-project-nzk9.onrender.com"},
    {"name": "📊 Population Tracker", "url": "https://population-project.onrender.com"},
    {"name": "📡 CPaaS Usage Monitor", "url": "https://synthetic-call-sms-activity-simulator.onrender.com"},
    {"name": "📈 Stock Monitor", "url": "https://stock-market-project-7kz6.onrender.com"},
]

STOCK_API_URL = "https://stock-market-project-7kz6.onrender.com/api/snapshot"
TELECOM_API_URL = "https://telecom-observer.onrender.com/api/snapshot"

# In-memory tracker for service-down + population alerts (resets on restart — acceptable, low-frequency events)
already_alerted = set()

# ---------------- DATABASE HELPERS ----------------

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_alert_state_table():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_state (
            id INTEGER PRIMARY KEY DEFAULT 1,
            last_calls_milestone BIGINT NOT NULL DEFAULT 0,
            last_sms_milestone BIGINT NOT NULL DEFAULT 0
        )
    """)
    cur.execute("SELECT COUNT(*) FROM alert_state")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO alert_state (id, last_calls_milestone, last_sms_milestone) VALUES (1, 0, 0)")

    # 👇 ADD THIS — per-stock price milestone tracking
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_alert_state (
            symbol TEXT PRIMARY KEY,
            last_price_milestone BIGINT NOT NULL DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            message TEXT NOT NULL,
            log_time TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_last_milestones():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT last_calls_milestone, last_sms_milestone FROM alert_state WHERE id=1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0], row[1]

def get_stock_price_milestone(symbol):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT last_price_milestone FROM stock_alert_state WHERE symbol=%s", (symbol,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO stock_alert_state (symbol, last_price_milestone) VALUES (%s, 0)", (symbol,))
        conn.commit()
        cur.close()
        conn.close()
        return 0
    cur.close()
    conn.close()
    return row[0]

def update_stock_price_milestone(symbol, milestone):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE stock_alert_state SET last_price_milestone=%s WHERE symbol=%s",
        (milestone, symbol)
    )
    conn.commit()
    cur.close()
    conn.close()
    
def update_last_milestones(calls_milestone, sms_milestone):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE alert_state SET last_calls_milestone=%s, last_sms_milestone=%s WHERE id=1",
        (calls_milestone, sms_milestone)
    )
    conn.commit()
    cur.close()
    conn.close()

def add_notification(message):
    if "MINOR" in message.upper():
        return  # skip minor alerts, don't store them
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO notifications (message, log_time) VALUES (%s, %s)",
        (message, now_ist())
    )
    cur.execute("""
        DELETE FROM notifications
        WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 10)
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_recent_notifications():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT message, log_time FROM notifications ORDER BY id DESC LIMIT 10")
    rows = [{"message": r[0], "time": r[1]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows
    
def get_live_data():
    conn = get_db()
    cur = conn.cursor()
    data = {}

    cur.execute("SELECT value, start_time FROM counter_state WHERE id=1")
    row = cur.fetchone()
    data["counter_value"] = row[0] if row else 0
    data["counter_started"] = row[1] if row else "N/A"

    cur.execute("SELECT state_name, population FROM population_state ORDER BY population DESC LIMIT 3")
    data["top_states"] = cur.fetchall()

    cur.execute("SELECT last_updated FROM population_meta WHERE id=1")
    row = cur.fetchone()
    data["population_last_updated"] = row[0] if row else "N/A"

    cur.execute("SELECT total_calls, total_sms FROM cpaas_totals WHERE id=1")
    row = cur.fetchone()
    data["total_calls"] = row[0] if row else 0
    data["total_sms"] = row[1] if row else 0

    cur.close()
    conn.close()

    # 📡 Telecom snapshot
    try:
        tel_resp = requests.get(TELECOM_API_URL, timeout=5)
        tel_json = tel_resp.json()
        data["telecom_summary"] = tel_json.get("summary")
        data["telecom_security"] = tel_json.get("security_events", [])[:3]
    except Exception:
        data["telecom_summary"] = None
        data["telecom_security"] = []
    
    # 📈 Stock Market snapshot — fetched over HTTP from the standalone stock service
    try:
        stock_resp = requests.get(STOCK_API_URL, timeout=5)
        stock_json = stock_resp.json()
        data["top_gainer"] = stock_json.get("top_gainer")
        data["top_loser"] = stock_json.get("top_loser")
        data["stocks_last_updated"] = stock_json.get("updated", "N/A")
    except Exception:
        data["top_gainer"] = None
        data["top_loser"] = None
        data["stocks_last_updated"] = "N/A"

    return data

def get_population_changes():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT state_name, change, log_time
        FROM population_history
        WHERE id IN (
            SELECT MAX(id) FROM population_history GROUP BY state_name
        )
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_stock_history_changes():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT sh.symbol, sh.price, sh.change_amount, sh.log_time, ss.open_price
        FROM stock_history sh
        JOIN stock_state ss ON ss.symbol = sh.symbol
        WHERE sh.id IN (
            SELECT MAX(id) FROM stock_history GROUP BY symbol
        )
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows
# ---------------- EMAIL + STATUS HELPERS ----------------

def send_alert_email(subject, body):
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
    if not BREVO_API_KEY or not GMAIL_ADDRESS or not ALERT_EMAIL_TO:
        print("Email not configured, skipping alert", flush=True)
        return

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }
    payload = {
        "sender": {"name": "Control Panel Alerts", "email": GMAIL_ADDRESS},
        "to": [{"email": ALERT_EMAIL_TO}],
        "subject": subject,
        "textContent": body
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            print(f"Alert email sent via Brevo: {subject}", flush=True)
        else:
            print(f"Brevo send failed: {response.status_code} - {response.text}", flush=True)
    except Exception as e:
        print(f"Failed to send email via Brevo: {e}", flush=True)

def check_status(url):
    try:
        r = requests.get(url, timeout=5)
        return ("🟢 Live", "lime") if r.status_code == 200 else ("🟡 Issue", "yellow")
    except:
        return ("🔴 Down", "red")

# ---------------- ALERT LOGIC ----------------

def run_alert_checks(service_statuses, live_data):
    # --- Check 1: Service down ---
    for name, status_text in service_statuses.items():
        alert_key = f"down_{name}"
        if "Down" in status_text:
            if alert_key not in already_alerted:
                subject = f"🔴 Incident: {name} is DOWN"
                send_alert_email(
                    subject,
                    f"Detected at {now_ist()} IST.\n\nService '{name}' is not responding."
                )
                add_notification(subject)
                already_alerted.add(alert_key)
        else:
            already_alerted.discard(alert_key)

    # --- Check 2: Population swings (per state, per update) ---
    try:
        changes = get_population_changes()
        for state_name, change, log_time in changes:
            alert_key = f"pop_{state_name}_{log_time}"
            if change <= POPULATION_DECREASE_LIMIT or change >= POPULATION_INCREASE_LIMIT:
                if alert_key not in already_alerted:
                    direction = "decreased" if change < 0 else "increased"
                    subject = f"⚠️ Incident: {state_name} population {direction} sharply"
                    send_alert_email(
                        subject,
                        f"Detected at {now_ist()} IST.\n\n{state_name} population changed by {change:+} "
                        f"in the latest 5-minute update (recorded at {log_time})."
                    )
                    add_notification(subject)
                    already_alerted.add(alert_key)
    except Exception as e:
        print(f"Population check failed: {e}", flush=True)

    # --- Check 3 & 4: Calls and SMS milestones (persisted in DB) ---
    last_calls_milestone, last_sms_milestone = get_last_milestones()

    total_calls = live_data["total_calls"]
    current_calls_milestone = total_calls // CALLS_MULTIPLE
    if current_calls_milestone > last_calls_milestone:
        subject = "📞 Incident: Call milestone reached"
        send_alert_email(
            subject,
            f"Detected at {now_ist()} IST.\n\nTotal calls reached {total_calls:,} "
            f"(crossed the {current_calls_milestone * CALLS_MULTIPLE:,} mark)."
        )
        add_notification(subject)

    total_sms = live_data["total_sms"]
    current_sms_milestone = total_sms // SMS_MULTIPLE
    if current_sms_milestone > last_sms_milestone:
        subject = "💬 Incident: SMS milestone reached"
        send_alert_email(
            subject,
            f"Detected at {now_ist()} IST.\n\nTotal SMS reached {total_sms:,} "
            f"(crossed the {current_sms_milestone * SMS_MULTIPLE:,} mark)."
        )
        add_notification(subject)

    update_last_milestones(current_calls_milestone, current_sms_milestone)

    # --- Check 5: Stock tick spikes + day-change limits + price milestones ---
    try:
        stock_rows = get_stock_history_changes()
        for symbol, price, change_amount, log_time, open_price in stock_rows:
            price = float(price); change_amount = float(change_amount); open_price = float(open_price)

            tick_pct = (change_amount / (price - change_amount) * 100) if (price - change_amount) else 0
            alert_key = f"tickspike_{symbol}_{log_time}"
            if abs(tick_pct) >= STOCK_TICK_SPIKE_PCT and alert_key not in already_alerted:
                direction = "jumped" if tick_pct > 0 else "dropped"
                subject = f"⚡ Incident: {symbol} {direction} sharply in one tick"
                send_alert_email(
                    subject,
                    f"Detected at {now_ist()} IST.\n\n{symbol} moved {change_amount:+.2f} ({tick_pct:+.2f}%) "
                    f"in a single tick (recorded at {log_time})."
                )
                add_notification(subject)
                already_alerted.add(alert_key)
             
          #  day_change_pct = ((price - open_price) / open_price * 100) if open_price else 0
          #  day_alert_key = f"daychange_{symbol}_{log_time}"
          #  if abs(day_change_pct) >= STOCK_DAY_CHANGE_LIMIT_PCT and day_alert_key not in already_alerted:
          #      direction = "risen" if day_change_pct > 0 else "fallen"
          #      subject = f"📉 Incident: {symbol} has {direction} {abs(day_change_pct):.2f}% today"
          #      send_alert_email(
          #          subject,
          #          f"Detected at {now_ist()} IST.\n\n{symbol} is now at ₹{price:,.2f}, "
          #          f"a {day_change_pct:+.2f}% change from today's open of ₹{open_price:,.2f}."
          #      )
          #      add_notification(subject)
          #      already_alerted.add(day_alert_key)

            last_milestone = get_stock_price_milestone(symbol)
            current_milestone = int(price // STOCK_PRICE_MILESTONE)
            if current_milestone != last_milestone:
                direction = "crossed above" if current_milestone > last_milestone else "dropped below"
                subject = f"💹 Incident: {symbol} {direction} ₹{current_milestone * STOCK_PRICE_MILESTONE:,}"
                send_alert_email(
                    subject,
                    f"Detected at {now_ist()} IST.\n\n{symbol} is now trading at ₹{price:,.2f}."
                )
                add_notification(subject)
                update_stock_price_milestone(symbol, current_milestone)
    except Exception as e:
        print(f"Stock check failed: {e}", flush=True)

    # --- Check 6: Telecom anomalies ---
    try:
        tel_resp = requests.get(TELECOM_API_URL, timeout=5)
        tel_json = tel_resp.json()
        for event in tel_json.get("security_events", []):
            if event["severity"] == "minor":
                continue  # skip minor telecom events
            alert_key = f"telecom_{event['source']}_{event['event_type']}_{event['time']}"
            if alert_key not in already_alerted:
                subject = f"{'🔴' if event['severity'] == 'critical' else '🟠' if event['severity'] == 'major' else '🟡'} Telecom {event['severity'].upper()}: {event['event_type']}"
                send_alert_email(subject,
                    f"Detected at {now_ist()} IST.\n\n"
                    f"Source: {event['source'].upper()}\n"
                    f"Event: {event['event_type']}\n"
                    f"Detail: {event['detail']}"
                )
                add_notification(subject)
                already_alerted.add(alert_key)
    except Exception as e:
        print(f"Telecom check failed: {e}", flush=True)
        
# ---------------- ROUTES ----------------

@app.route("/run-checks")
def run_checks():
    service_statuses = {}
    for s in services:
        status_text, _ = check_status(s["url"])
        service_statuses[s["name"]] = status_text
    try:
        live = get_live_data()
        run_alert_checks(service_statuses, live)
        return f"Checks completed at {now_ist()} IST", 200
    except Exception as e:
        return f"Check failed: {e}", 500

@app.route("/test-email")
def test_email():
    send_alert_email(
        "✅ Test Email from Control Panel",
        f"This is a test email sent at {now_ist()} IST to confirm the email pipeline works."
    )
    return "Test email attempt finished — check Render logs and your inbox", 200

@app.route("/api/dashboard-data")
def dashboard_data():
    service_list = []
    service_statuses = {}
    for s in services:
        status_text, color = check_status(s["url"])
        service_statuses[s["name"]] = status_text
        service_list.append({"name": s["name"], "url": s["url"], "status": status_text, "color": color})

    notifications = get_recent_notifications()

    try:
        live = get_live_data()
        top_states = [{"name": name, "population": pop} for name, pop in live["top_states"]]
        return jsonify({
            "checked_at": now_ist(),
            "services": service_list,
            "counter_value": live["counter_value"],
            "total_calls": live["total_calls"],
            "total_sms": live["total_sms"],
            "top_states": top_states,
            "population_last_updated": live["population_last_updated"],
            "top_gainer": live.get("top_gainer"),
            "top_loser": live.get("top_loser"),
            "stocks_last_updated": live.get("stocks_last_updated"),
            "telecom_summary": live.get("telecom_summary"),
            "telecom_security": live.get("telecom_security", []),
            "notifications": get_recent_notifications(),
            "error": None
        })
    except Exception as e:
        return jsonify({
            "checked_at": now_ist(),
            "services": service_list,
            "notifications": notifications,
            "error": str(e)
        })
        
#@app.route("/fix-notifications")
 #def fix_notifications():
    #conn = get_db()
    #cur = conn.cursor()
    #cur.execute("DELETE FROM notifications")
    #conn.commit()
    #cur.close()
    #conn.close()
    #return "Notifications cleared", 200
 
@app.route("/")
def dashboard():
    return """
    <html>
    <head><title>My Cloud Dashboard</title></head>
    <body style="font-family:monospace; background:#111; color:#0f0; padding:40px">
        <h1>🏠 My Cloud Dashboard</h1>
        <p style="color:#aaa">Last checked (IST): <span id="last-checked">loading...</span></p>
        <h3>📡 Service Status</h3>
        <table style="border-collapse:collapse; width:100%; margin-top:10px">
            <tr style="border-bottom:1px solid #0f0">
                <th style="text-align:left; padding:12px">Service</th>
                <th style="text-align:left; padding:12px">Status</th>
                <th style="text-align:left; padding:12px">Link</th>
            </tr>
            <tbody id="service-rows"></tbody>
        </table>
        <h3 style="margin-top:30px">📊 Live Data Snapshot</h3>
        <div id="data-panel">Loading...</div>
        <p style="color:#666; margin-top:30px">Data refreshes every 12 seconds. Background checks every 5 min via cron-job.org.</p>

        <div id="notif-box" style="position:fixed; bottom:16px; right:16px; width:240px; max-height:200px;
            overflow-y:auto; background:#0f0f0f; border:1px solid #1e1e1e; border-radius:6px;
            padding:10px 12px; box-shadow:0 2px 8px rgba(0,0,0,0.5); font-size:11px; z-index:100;">
            <div style="color:#888; font-weight:bold; margin-bottom:6px; font-size:11px;">🔔 Recent Alerts</div>
            <ul id="notif-list" style="margin:0; padding-left:14px; color:#777; list-style:disc;"></ul>
        </div>

        <script>
        async function refresh() {
            try {
                const res = await fetch('/api/dashboard-data');
                const data = await res.json();

                document.getElementById('last-checked').textContent = data.checked_at;

                document.getElementById('service-rows').innerHTML = data.services.map(s => `
                    <tr>
                        <td style="padding:12px">${s.name}</td>
                        <td style="padding:12px; color:${s.color}">${s.status}</td>
                        <td style="padding:12px"><a href="${s.url}" target="_blank" style="color:cyan">Open →</a></td>
                    </tr>`).join('');

                const notifList = document.getElementById('notif-list');
                if (data.notifications && data.notifications.length > 0) {
                    notifList.innerHTML = data.notifications.map(n => `
                        <li style="margin-bottom:4px; font-size:10px; line-height:1.3;">
                            ${n.message}<br><span style="color:#555; font-size:9px;">${n.time}</span>
                        </li>`).join('');
                } else {
                    notifList.innerHTML = '<li style="list-style:none; margin-left:-18px; color:#555;">No alerts yet</li>';
                }

                if (data.error) {
                    document.getElementById('data-panel').innerHTML =
                        `<p style='color:red'>⚠️ Could not load live data: ${data.error}</p>`;
                    return;
                }

                const topStatesHtml = data.top_states.map(s => `<li>${s.name}: ${s.population.toLocaleString()}</li>`).join('');
                const gainer = data.top_gainer;
                const loser = data.top_loser;
                const gainerHtml = gainer ? `${gainer.symbol} ${gainer.change_pct >= 0 ? '+' : ''}${gainer.change_pct.toFixed(2)}%` : 'N/A';
                const loserHtml = loser ? `${loser.symbol} ${loser.change_pct >= 0 ? '+' : ''}${loser.change_pct.toFixed(2)}%` : 'N/A';

                const tel = data.telecom_summary;

                const telHtml = tel
                    ? `<div><p style="color:#aaa">Telecom Critical</p><h2 style="color:#ff3333">${tel.critical_events}</h2></div>
                       <div><p style="color:#aaa">Telecom Major</p><h2 style="color:#ff8800">${tel.major_events}</h2></div>
                       <div><p style="color:#aaa">Telecom Minor</p><h2 style="color:#ffff00">${tel.minor_events}</h2></div>`
                    : '';
                    
                document.getElementById('data-panel').innerHTML = `
                    <div style="display:flex; gap:50px; margin:25px 0; flex-wrap:wrap;">
                        <div><p style="color:#aaa">Counter Value</p><h2 style="color:lime">${data.counter_value}</h2></div>
                        <div><p style="color:#aaa">Total CPaaS Calls</p><h2 style="color:yellow">${data.total_calls.toLocaleString()}</h2></div>
                        <div><p style="color:#aaa">Total CPaaS SMS</p><h2 style="color:orange">${data.total_sms.toLocaleString()}</h2></div>
                        <div><p style="color:#aaa">Top 3 States</p><ul style="color:cyan">${topStatesHtml}</ul></div>
                        <div><p style="color:#aaa">Top Stock Gainer</p><h2 style="color:lime">${gainerHtml}</h2></div>
                        <div><p style="color:#aaa">Top Stock Loser</p><h2 style="color:#ff5050">${loserHtml}</h2></div>
                        ${telHtml}
                    </div>
                    <p style="margin-top:15px"><a href="https://counter-project-nzk9.onrender.com/dbview" target="_blank" style="color:cyan">🗄️ View Database</a></p>
                    <p style="color:#666">Population last updated: ${data.population_last_updated} &nbsp;|&nbsp; Stocks last updated: ${data.stocks_last_updated}</p>
                `;
            } catch (e) {
                console.error('Refresh failed:', e);
            }
        }
        refresh();
        setInterval(refresh, 12000);
        </script>
    </body></html>"""

@app.route("/dbview")
def dbview():
    provided_password = request.args.get("password", "")
    if provided_password != DBVIEW_PASSWORD:
        return """
        <html>
        <head><title>Locked</title></head>
        <body style="font-family:monospace; background:#111; color:#0f0; padding:60px; text-align:center;">
            <h2>🔒 Access Restricted</h2>
            <p>Add ?password=YOUR_PASSWORD to the URL to view this page.</p>
        </body></html>
        """, 401

    # --- Read filter parameters from URL ---
    search_text = request.args.get("search", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    table_filter = request.args.get("table", "all")
    row_limit_raw = request.args.get("limit", "200").strip()

    # Validate row limit
    if row_limit_raw == "all":
        row_limit = None
    else:
        try:
            row_limit = int(row_limit_raw)
            if row_limit <= 0:
                row_limit = 200
        except ValueError:
            row_limit = 200

    conn = get_db()
    cur = conn.cursor()
    sections = []

    table_list = [
    "counter_state", "counter_logs",
    "population_state", "population_history",
    "cpaas_totals", "cpaas_minute_stats",
    "stock_state", "stock_history",
    "telecom_traffic", "telecom_dlr",
    "telecom_security", "telecom_baselines"
]
    
    for table_name in table_list:
        if table_filter != "all" and table_filter != table_name:
            continue

        has_log_time = table_name in (
        "counter_logs",
        "population_history",
        "cpaas_minute_stats",
        "stock_history",
        "telecom_traffic",
        "telecom_dlr",
        "telecom_security"
    )
        
        if has_log_time:
            query = f"SELECT * FROM {table_name} WHERE 1=1"
            params = []
            if date_from:
                query += " AND log_time >= %s"
                params.append(date_from)
            if date_to:
                query += " AND log_time <= %s"
                params.append(date_to + " 23:59:59")
            if search_text:
                query += " AND log_time::text ILIKE %s"
                params.append(f"%{search_text}%")
            query += " ORDER BY id DESC"
            if row_limit is not None:
                query += " LIMIT %s"
                params.append(row_limit)
            cur.execute(query, params)
        else:
            cur.execute(f"SELECT * FROM {table_name} ORDER BY 1")

        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        sections.append((table_name, cols, rows))

    cur.close()
    conn.close()

    # --- Build filter form ---
    table_options = "".join(
        f'<option value="{t}" {"selected" if table_filter==t else ""}>{t}</option>'
        for t in table_list
    )

    limit_options_list = ["20", "50", "200", "500", "1000", "all"]
    limit_options = "".join(
        f'<option value="{l}" {"selected" if row_limit_raw==l else ""}>{"All" if l=="all" else l}</option>'
        for l in limit_options_list
    )

    filter_html = f"""
    <form method="GET" style="margin-bottom:25px; background:#1a1a1a; padding:15px; border-radius:6px;">
        <input type="hidden" name="password" value="{provided_password}">
        <label>Table:
            <select name="table">
                <option value="all" {"selected" if table_filter=="all" else ""}>All Tables</option>
                {table_options}
            </select>
        </label>
        &nbsp;&nbsp;
        <label>Show:
            <select name="limit">{limit_options}</select> rows
        </label>
        &nbsp;&nbsp;
        <label>Search (timestamp text): <input type="text" name="search" value="{search_text}" placeholder="e.g. 2026-06-17"></label>
        &nbsp;&nbsp;
        <label>From: <input type="date" name="date_from" value="{date_from}"></label>
        &nbsp;&nbsp;
        <label>To: <input type="date" name="date_to" value="{date_to}"></label>
        &nbsp;&nbsp;
        <button type="submit" style="background:#0a5;color:white;border:none;padding:6px 14px;cursor:pointer;border-radius:4px;">Apply Filters</button>
        <a href="/dbview?password={provided_password}" style="color:cyan; margin-left:10px;">Clear Filters</a>
    </form>
    """

    html = f"""
    <html>
    <head>
        <title>Database Viewer</title>
        <style>
            body {{ font-family:monospace; background:#111; color:#0f0; padding:30px; }}
            h3 {{ color:cyan; margin-top:30px; }}
            table {{ border-collapse:collapse; width:100%; margin-bottom:10px; }}
            td, th {{ padding:6px 10px; text-align:left; border-bottom:1px solid #333; font-size:13px; }}
            th {{ color:yellow; }}
            input, select {{ background:#222; color:#0f0; border:1px solid #444; padding:4px; }}
            label {{ color:#aaa; }}
        </style>
    </head>
    <body>
        <h2>🗄️ Database Viewer (Read-Only)</h2>
        <p style="color:#aaa">Showing tables from shared-logs-db</p>
        {filter_html}
    """

    for table_name, cols, rows in sections:
        html += f"<h3>📋 {table_name} ({len(rows)} rows shown)</h3>"
        html += "<table><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
        for row in rows:
            html += "<tr>" + "".join(f"<td>{val}</td>" for val in row) + "</tr>"
        html += "</table>"

    html += "</body></html>"
    return html
    
if __name__ == "__main__":
    init_alert_state_table()
    app.run(host="0.0.0.0", port=10000, threaded=True)
