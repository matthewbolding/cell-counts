#!/bin/sh
# One-shot release step, run by the `migrate` service before `web` starts:
# apply migrations, make sure the cache table exists, and publish this image's
# collected static files to the volume nginx serves /static/ from.
set -eu

python manage.py migrate --noinput
python manage.py createcachetable

# Copy (don't wipe first): a browser tab opened before this release may still
# ask for the previous release's hashed asset names. Files no release has
# rewritten for 30 days are pruned.
cp -R /app/staticfiles/. /srv/static/
find /srv/static -type f -mtime +30 -delete

echo "release: migrations applied, static files published"
