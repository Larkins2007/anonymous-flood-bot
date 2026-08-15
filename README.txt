# Render Keepalive

Free workaround for Render Web Service idle spin-down.

Copy `.github/workflows/render-keepalive.yml` into the repository.
The workflow pings the Render service every 10 minutes.

No BOT_TOKEN or other secrets are used.
The bot.py does not need to be changed.

After pushing:
1. Open GitHub -> Actions.
2. Select "Render Keepalive".
3. Run it manually once with "Run workflow".
4. Leave the workflow enabled.

Target:
https://anonymous-flood-bot.onrender.com/

