# MacFire AI Scout — Live Dashboard

## Run locally (test on your Mac)

```bash
cd ~/Documents/MacFire/Dashboard
pip3 install flask requests
python3 app.py
# Open: http://localhost:5000
```

## Get a shareable link (free, 5 minutes)

### Option A — Render.com (permanent URL, recommended)

1. Go to https://render.com and sign up (free)
2. New → Web Service → "Deploy from existing code"
3. Upload the Dashboard folder, or push it to a GitHub repo
4. Set:
   - Build command:  `pip install -r requirements.txt`
   - Start command:  `gunicorn app:app`
5. Add environment variables:
   - `NOTION_TOKEN` = your Notion token
   - `NOTION_DB_ID` = your database ID
6. Deploy → you get a URL like `https://macfire-dashboard.onrender.com`

### Option B — Run locally + share with ngrok

```bash
# Install ngrok from https://ngrok.com (free account)
python3 app.py &         # start the server
ngrok http 5000          # creates public URL, e.g. https://abc123.ngrok.io
```

Share the ngrok URL. Works while your Mac is on.
