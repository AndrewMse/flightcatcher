# Playwright's own image already carries Chromium and its system libraries,
# which are otherwise the fiddly part of running this on a Pi or a VPS.
# The noble (Ubuntu 24.04) variant, because it ships Python 3.12; the jammy
# images are on 3.10, which this project does not support.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

COPY pyproject.toml README.md ./
COPY hopwatch ./hopwatch

# Pin Playwright to the image's version so it uses the Chromium already in
# /ms-playwright instead of downloading another one.
RUN pip install --no-cache-dir --break-system-packages '.[service,aws]' 'playwright==1.63.0' \
 && playwright install chromium

# State lives on a volume: the SQLite database and, more importantly, the
# browser profile holding the logged-in session. HOME points there so the
# default paths under ~/.local/share/hopwatch survive restarts -- including
# the ones the auto-updater does. (Chromium itself lives in /ms-playwright.)
ENV HOME=/state \
    HOPWATCH_CONFIG=/config/config.toml \
    HOPWATCH_DB=/state/hopwatch.db
VOLUME ["/state", "/config"]

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=5)"

# Note: `hopwatch login` needs a visible browser, so seed the profile on a
# machine with a display and copy the profile directory into the volume, or run
# this container once with an X server attached.
CMD ["hopwatch", "serve", "--host", "0.0.0.0"]
