# Playwright's own image already carries Chromium and its system libraries,
# which are otherwise the fiddly part of running this on a Pi or a VPS.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY pyproject.toml README.md ./
COPY hopwatch ./hopwatch

RUN pip install --no-cache-dir '.[service]' \
 && playwright install chromium

# State lives on a volume: the SQLite database and, more importantly, the
# browser profile holding the logged-in session.
ENV HOPWATCH_CONFIG=/config/config.toml
VOLUME ["/state", "/config"]

EXPOSE 8765

# Note: `hopwatch login` needs a visible browser, so seed the profile on a
# machine with a display and copy the profile directory into the volume, or run
# this container once with an X server attached.
CMD ["hopwatch", "serve", "--host", "0.0.0.0"]
