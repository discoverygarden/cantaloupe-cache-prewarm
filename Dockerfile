FROM python:3.13-slim

COPY warm_mirador_v3.py /usr/local/bin/warm_mirador_v3.py
RUN chmod +x /usr/local/bin/warm_mirador_v3.py

ENTRYPOINT ["python3", "/usr/local/bin/warm_mirador_v3.py"]
