from flask import Flask
import requests
import datetime
from datetime import timezone, timedelta

app = Flask(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

# 👇 PASTE YOUR REAL URLS HERE
services = [
    {"name": "🔢 Counter Server", "url": "https://counter-project-nzk9.onrender.com/"},
    {"name": "📊 Population Tracker", "url": "https://population-project.onrender.com/"},
    {"name": "📡 CPaaS Usage Monitor", "url": "https://synthetic-call-sms-activity-simulator.onrender.com/"},
]

def check_status(url):
    try:
        r = requests.get(url, timeout=5)
        return ("🟢 Live", "lime") if r.status_code == 200 else ("🟡 Issue", "yellow")
    except:
        return ("🔴 Down", "red")

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

    return f"""
    <html>
    <head><title>My Cloud Dashboard</title><meta http-equiv="refresh" content="30"></head>
    <body style="font-family:monospace; background:#111; color:#0f0; padding:40px">
        <h1>🏠 My Cloud Dashboard</h1>
        <p style="color:#aaa">Last checked (IST): {now_ist()}</p>
        <table style="border-collapse:collapse; width:100%; margin-top:20px">
            <tr style="border-bottom:1px solid #0f0">
                <th style="text-align:left; padding:12px">Service</th>
                <th style="text-align:left; padding:12px">Status</th>
                <th style="text-align:left; padding:12px">Link</th>
            </tr>
            {rows}
        </table>
        <p style="color:#666; margin-top:30px">Auto-refreshes every 30 seconds</p>
    </body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, threaded=True)
