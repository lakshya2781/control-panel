from flask import Flask
import requests
import datetime, os
from datetime import timezone, timedelta
import psycopg2

app = Flask(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

DATABASE_URL = os.environ.get("DATABASE_URL")

services = [
    {"name": "🔢 Counter Server", "url": "https://counter-project-nzk9.onrender.com"},
    {"name": "📊 Population Tracker", "url": "https://population-project.onrender.com/"},
    {"name": "📡 CPaaS Usage Monitor", "url": "https://synthetic-call-sms-activity-simulator.onrender.com/"},
]

def get_db():
    return psycopg2.connect(DATABASE_URL)

def check_status(url):
    try:
        r = requests.get(url, timeout=5)
        return ("🟢 Live", "lime") if r.status_code == 200 else ("🟡 Issue", "yellow")
    except:
        return ("🔴 Down", "red")

def get_live_data():
    conn = get_db()
    cur = conn.cursor()
    data = {}

    # Counter
    cur.execute("SELECT value, start_time FROM counter_state WHERE id=1")
    row = cur.fetchone()
    data["counter_value"] = row[0] if row else "N/A"
    data["counter_started"] = row[1] if row else "N/A"

    # Population — top 3 states by population
    cur.execute("SELECT state_name, population FROM population_state ORDER BY population DESC LIMIT 3")
    data["top_states"] = cur.fetchall()

    cur.execute("SELECT last_updated FROM population_meta WHERE id=1")
    row = cur.fetchone()
    data["population_last_updated"] = row[0] if row else "N/A"

    # CPaaS totals
    cur.execute("SELECT total_calls, total_sms FROM cpaas_totals WHERE id=1")
    row = cur.fetchone()
    data["total_calls"] = row[0] if row else 0
    data["total_sms"] = row[1] if row else 0

    cur.close()
    conn.close()
    return data

@app.route("/")
def dashboard():
    rows = ""
    for s in services:
        status_text, color = check_status(s["url"])
        rows += f"""
        <tr>
            <td style="padding:12px">{s['name']}</td>
            <td style="padding:12px; color:{color}">{status_text}</td>
            <td style="padding:12px"><a href="{s['url']}" target="_blank" style="color:cyan">Open →</a></td>
        </tr>"""

    try:
        live = get_live_data()
        top_states_html = "".join(
            f"<li>{name}: {pop:,}</li>" for name, pop in live["top_states"]
        )
        data_panel = f"""
        <div style="display:flex; gap:50px; margin:25px 0; flex-wrap:wrap;">
            <div>
                <p style="color:#aaa">Counter Value</p>
                <h2 style="color:lime">{live['counter_value']}</h2>
            </div>
            <div>
                <p style="color:#aaa">Total CPaaS Calls</p>
                <h2 style="color:yellow">{live['total_calls']:,}</h2>
            </div>
            <div>
                <p style="color:#aaa">Total CPaaS SMS</p>
                <h2 style="color:orange">{live['total_sms']:,}</h2>
            </div>
            <div>
                <p style="color:#aaa">Top 3 States by Population</p>
                <ul style="color:cyan">{top_states_html}</ul>
            </div>
        </div>
        <p style="color:#666">Population last updated: {live['population_last_updated']}</p>
        """
    except Exception as e:
        data_panel = f"<p style='color:red'>⚠️ Could not load live data: {e}</p>"

    return f"""
    <html>
    <head><title>My Cloud Dashboard</title><meta http-equiv="refresh" content="30"></head>
    <body style="font-family:monospace; background:#111; color:#0f0; padding:40px">
        <h1>🏠 My Cloud Dashboard</h1>
        <p style="color:#aaa">Last checked (IST): {now_ist()}</p>

        <h3>📡 Service Status</h3>
        <table style="border-collapse:collapse; width:100%; margin-top:10px">
            <tr style="border-bottom:1px solid #0f0">
                <th style="text-align:left; padding:12px">Service</th>
                <th style="text-align:left; padding:12px">Status</th>
                <th style="text-align:left; padding:12px">Link</th>
            </tr>
            {rows}
        </table>

        <h3 style="margin-top:30px">📊 Live Data Snapshot</h3>
        {data_panel}

        <p style="color:#666; margin-top:30px">Auto-refreshes every 30 seconds</p>
    </body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, threaded=True)
        
