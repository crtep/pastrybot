"""Add crtep.com/pastrybot to the Caddyfile (proxied to the pastrybot service), once.

Run with sudo. Keeps a backup at Caddyfile.bak; run `caddy validate` before reloading.
"""

import shutil
import sys
from pathlib import Path

CADDYFILE = Path("/etc/caddy/Caddyfile")
SITE = "crtep.com {"
ROUTE = """
  # pastrybot: the app is served at /, so strip the /pastrybot prefix
  redir /pastrybot /pastrybot/
  handle_path /pastrybot/* {
    reverse_proxy 127.0.0.1:8650
  }
"""

text = CADDYFILE.read_text()
if "handle_path /pastrybot/*" in text:
    sys.exit("The pastrybot route is already in the Caddyfile; nothing to do.")
if text.count(SITE) != 1:
    sys.exit(f"Expected exactly one '{SITE}' block in {CADDYFILE}; not editing it.")
shutil.copy(CADDYFILE, CADDYFILE.with_name("Caddyfile.bak"))
CADDYFILE.write_text(text.replace(SITE, SITE + ROUTE, 1))
print(f"Added the /pastrybot route to {CADDYFILE} (backup: Caddyfile.bak).")
