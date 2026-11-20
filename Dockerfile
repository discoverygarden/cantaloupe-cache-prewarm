FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

COPY warm_mirador_v3.py /usr/local/bin/warm_mirador_v3.py
RUN chmod +x /usr/local/bin/warm_mirador_v3.py

ENTRYPOINT ["python3", "/usr/local/bin/warm_mirador_v3.py"]

# --------------------------------------
# Package manifest: prints every installed package to the build log as
#   DGI-PKG <arch> <type> <name> <version>
# so security checks can read versions from the image build's job log.
# Keep this as the last step of the final stage.
# --------------------------------------
RUN a=$(uname -m); \
  dpkg-query -W -f='${Package} ${Version}\n' | sed "s/^/DGI-PKG $a deb /"; \
  pip list --format=freeze 2>/dev/null | sed "s/==/ /; s/^/DGI-PKG $a pip /"; \
  true
