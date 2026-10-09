# NOTE: Importing `bot` from bot.py executes the ENTIRE module-level logic of bot.py:
#  • MongoDB connection (with retry + backoff)
#  • Safe token validation via Telegram get_me()
#  • Loading users_data / settings / pending_verifications etc. from Mongo/JSON
#  • Auto-save / auto-clean threads started
#  • Handlers registered
# It does NOT start polling (that happens inside `safe_start_polling` below).
import bot as _bot_module
from bot import bot, config, save_all_data
import telebot
from flask import Flask, request
import threading
import os
import sys
import time

import logging

# Flask app for generic hosting (Railway / Render / Replit / custom VPS / etc.)
app = Flask(__name__)

# Clean logs: Set Flask and Werkzeug to WARNING only
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)
app.logger.setLevel(logging.WARNING)

# ---------------------- Health / Status endpoints (safe, no secrets) ----------------------
@app.route('/')
def home():
    s = config.startup_status
    body = {
        "status": "ok",
        "bot": {
            "token_valid": bool(s.get("bot_token_valid")),
            "initialized": bool(s.get("bot_initialized")),
        },
        "mongodb": {
            "connected": bool(s.get("mongo_connected")),
            "attempts": int(s.get("mongo_attempts", 0)),
        },
        "config": {
            "source_mongo": bool(s.get("config_loaded_from_mongo")),
            "source_json": bool(s.get("config_loaded_from_json")),
        },
    }
    # Return HTML so human-visitor friendly, JSON via Accept header
    from flask import jsonify, request as _req
    if "application/json" in (_req.headers.get("Accept") or ""):
        return jsonify(body), 200
    return (
        "<h1>Premium Bot is running</h1>"
        f"<p>Token: <b>{'OK ✅' if body['bot']['token_valid'] else 'deferred ⏳'}</b> "
        f"| MongoDB: <b>{'OK ✅' if body['mongodb']['connected'] else 'JSON fallback ⚠️'}</b></p>"
        f"<p>Users: <code>{len(_bot_module.users_data)}</code> "
        f"| Settings: <code>{'MongoDB' if body['config']['source_mongo'] else 'Local JSON'}</code></p>"
        f"<p><a href=\"/health\">/health</a> &nbsp; <a href=\"/healthz\">/healthz</a></p>"
    ), 200

@app.route('/health')
@app.route('/healthz')
def health():
    s = config.startup_status
    # Basic health: bot module loaded + token valid OR token validation deferred
    healthy = True
    code = 200
    detail = []
    if s.get("mongo_connected"):
        detail.append("mongodb=ok")
    else:
        detail.append("mongodb=degraded(json_fallback)")
    if s.get("bot_token_valid"):
        detail.append("token=validated")
    else:
        detail.append("token=deferred")
    detail.append(f"users={len(_bot_module.users_data)}")
    return (
        "OK\n" if healthy else "DEGRADED\n",
        code if healthy else 200,  # Always 200 so orchestrators don't kill it for transient mongo issue
        {"Content-Type": "text/plain"},
    )

# ---------------------- Graceful shutdown helper (optional) ----------------------
def _graceful_shutdown():
    try:
        save_all_data()
        logging.info("app.py shutdown hook: saved all data.")
    except Exception as e:
        logging.error(f"app.py shutdown hook save failed: {e}")

import atexit as _atexit
_atexit.register(_graceful_shutdown)

# ---------------------- Start bot polling in a background thread ----------------------
def run_bot():
    """Delegate to bot.py's safe_start_polling (duplicate-protected)."""
    try:
        if hasattr(_bot_module, "safe_start_polling"):
            _bot_module.safe_start_polling()
        else:
            # Compatibility fallback — very unlikely since we control the repo
            logging.warning("app.py: safe_start_polling not found; using direct infinity_polling.")
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                allowed_updates=["message", "callback_query", "chat_member", "chat_join_request"]
            )
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.critical(f"app.py run_bot crashed: {type(e).__name__}: {e}")
        # Try to persist data before we let the thread die
        try:
            save_all_data()
        except Exception as _e2:
            logging.error(f"app.py run_bot final save also failed: {_e2}")
        # Don't silently swallow forever; host will restart the container
        time.sleep(5)
        sys.stderr.flush()
        # Exit the whole process so hosting provider restarts with exponential backoff
        os._exit(77)

_bot_thread = threading.Thread(target=run_bot, daemon=False, name="telegram-bot-polling")
_bot_thread.start()

# ---------------------- Optional Webhook endpoint ----------------------
# Note: If you switch from polling to webhooks, STOP the polling thread above.
# For the token-replacement workflow, polling is the simpler, recommended path.
@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json(silent=True)
    if not update:
        return "BAD", 400
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK", 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logging.info(f"Starting Flask on port {port}; bot thread is_alive={_bot_thread.is_alive()}")
    # threaded=True so multiple requests work; note: using a production WSGI
    # server (gunicorn via Procfile) is recommended for real traffic.
    app.run(host='0.0.0.0', port=port, threaded=True)
