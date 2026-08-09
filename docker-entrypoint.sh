#!/bin/sh
set -eu

# The development bind mount may be owned by root and have private permissions.
# Make only the application source readable/traversable by the non-root worker;
# do not touch .env or other secret-bearing files.
chmod a+rx /app
chmod a+r /app/tactic.py
chmod -R a+rX /app/arena_tactic

# A bind-mounted runtime directory is created by Docker as root on a fresh host.
# Fix its ownership once, then keep the actual worker non-root.
mkdir -p /app/runtime
chown -R app:app /app/runtime

exec su -s /bin/sh app -c 'exec python /app/tactic.py'
