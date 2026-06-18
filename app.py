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
STOCK_DAY_CHANGE_LIMIT_PCT = 50.0  # alert if any stock's day change crosses ±50%
STOCK_PRICE_MILESTONE = 1000       # alert every time a stock price crosses another ₹1000 mark

# 👇 EDIT THESE WITH YOUR REAL LIVE URLS
services = [
    {"name": "🔢 Counter Server", "url": "https://counter-project-nzk9.onrender.com"},
    {"name": "📊 Population Tracker", "url": "https://population-project.onrender.com"},
    {"name": "📡 CPaaS Usage Monitor", "url": "https://synthetic-call-sms-activity-simulator.onrender.com"},
    {"name": "📈 Stock Monitor", "url": "https://stock-market-project-7kz6.onrender.com"},
]

STOCK_API_URL = "https://stock-market-project-7kz6.onrender.com/api/snapshot"

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
                send_alert_email(
                    f"🔴 Incident: {name} is DOWN",
                    f"Detected at {now_ist()} IST.\n\nService '{name}' is not responding."
                )
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
                    send_alert_email(
                        f"⚠️ Incident: {state_name} population {direction} sharply",
                        f"Detected at {now_ist()} IST.\n\n{state_name} population changed by {change:+} "
                        f"in the latest 5-minute update (recorded at {log_time})."
                    )
                    already_alerted.add(alert_key)
    except Exception as e:
        print(f"Population check failed: {e}", flush=True)

    # --- Check 3 & 4: Calls and SMS milestones (persisted in DB) ---
    last_calls_milestone, last_sms_milestone = get_last_milestones()

    total_calls = live_data["total_calls"]
    current_calls_milestone = total_calls // CALLS_MULTIPLE
    if current_calls_milestone > last_calls_milestone:
        send_alert_email(
            "📞 Incident: Call milestone reached",
            f"Detected at {now_ist()} IST.\n\nTotal calls reached {total_calls:,} "
            f"(crossed the {current_calls_milestone * CALLS_MULTIPLE:,} mark)."
        )

    total_sms = live_data["total_sms"]
    current_sms_milestone = total_sms // SMS_MULTIPLE
    if current_sms_milestone > last_sms_milestone:
        send_alert_email(
            "💬 Incident: SMS milestone reached",
            f"Detected at {now_ist()} IST.\n\nTotal SMS reached {total_sms:,} "
            f"(crossed the {current_sms_milestone * SMS_MULTIPLE:,} mark)."
        )

    update_last_milestones(current_calls_milestone, current_sms_milestone)

    # --- Check 5: Stock tick spikes + day-change limits ---
    try:
        stock_rows = get_stock_history_changes()
        for symbol, price, change_amount, log_time, open_price in stock_rows:
            price = float(price); change_amount = float(change_amount); open_price = float(open_price)

            # Per-tick spike check
            tick_pct = (change_amount / (price - change_amount) * 100) if (price - change_amount) else 0
            alert_key = f"tickspike_{symbol}_{log_time}"
            if abs(tick_pct) >= STOCK_TICK_SPIKE_PCT and alert_key not in already_alerted:
                direction = "jumped" if tick_pct > 0 else "dropped"
                send_alert_email(
                    f"⚡ Incident: {symbol} {direction} sharply in one tick",
                    f"Detected at {now_ist()} IST.\n\n{symbol} moved {change_amount:+.2f} ({tick_pct:+.2f}%) "
                    f"in a single tick (recorded at {log_time})."
                )
                already_alerted.add(alert_key)

            # Day-change limit check
            day_change_pct = ((price - open_price) / open_price * 100) if open_price else 0
            day_alert_key = f"daychange_{symbol}_{log_time}"
            if abs(day_change_pct) >= STOCK_DAY_CHANGE_LIMIT_PCT and day_alert_key not in already_alerted:
                direction = "risen" if day_change_pct > 0 else "fallen"
                send_alert_email(
                    f"📉 Incident: {symbol} has {direction} {abs(day_change_pct):.2f}% today",
                    f"Detected at {now_ist()} IST.\n\n{symbol} is now at ₹{price:,.2f}, "
                    f"a {day_change_pct:+.2f}% change from today's open of ₹{open_price:,.2f}."
                )
                already_alerted.add(day_alert_key)

            # Price milestone check (persisted per symbol)
            open_price = float(open_price)
            stock_milestone = STOCK_PRICE_MILESTONE
            last_milestone = get_stock_price_milestone(symbol)
            current_milestone = int(price // STOCK_PRICE_MILESTONE)
            if current_milestone != last_milestone:
                direction = "crossed above" if current_milestone > last_milestone else "dropped below"
                send_alert_email(
                    f"💹 Incident: {symbol} {direction} ₹{current_milestone * STOCK_PRICE_MILESTONE:,}",
                    f"Detected at {now_ist()} IST.\n\n{symbol} is now trading at ₹{price:,.2f},"
                    f"a ₹{stock_milestone:,.2f} differ from today's open of ₹{open_price:,.2f}."
                )
                update_stock_price_milestone(symbol, current_milestone)
    except Exception as e:
        print(f"Stock check failed: {e}", flush=True)

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

    try:
        live = get_live_data()
        run_alert_checks(service_statuses, live)
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
            "error": None
        })
    except Exception as e:
        return jsonify({
            "checked_at": now_ist(),
            "services": service_list,
            "error": str(e)
        })
        
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

                document.getElementById('data-panel').innerHTML = `
                    <div style="display:flex; gap:50px; margin:25px 0; flex-wrap:wrap;">
                        <div><p style="color:#aaa">Counter Value</p><h2 style="color:lime">${data.counter_value}</h2></div>
                        <div><p style="color:#aaa">Total CPaaS Calls</p><h2 style="color:yellow">${data.total_calls.toLocaleString()}</h2></div>
                        <div><p style="color:#aaa">Total CPaaS SMS</p><h2 style="color:orange">${data.total_sms.toLocaleString()}</h2></div>
                        <div><p style="color:#aaa">Top 3 States</p><ul style="color:cyan">${topStatesHtml}</ul></div>
                        <div><p style="color:#aaa">Top Stock Gainer</p><h2 style="color:lime">${gainerHtml}</h2></div>
                        <div><p style="color:#aaa">Top Stock Loser</p><h2 style="color:#ff5050">${loserHtml}</h2></div>
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
 
if __name__ == "__main__":
    init_alert_state_table()
    app.run(host="0.0.0.0", port=10000, threaded=True)
