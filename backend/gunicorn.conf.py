"""Single worker on purpose: the scheduler lives inside the web process, so more
than one worker would scan and push the same bet several times (a bug we already
paid for once on the trading bot)."""
import multiprocessing  # noqa: F401
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 1
threads = 4
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
preload_app = False
