# Container build (NOT deployed). Any host that runs Docker works: Render, Fly.io, Railway, a $5 VPS.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 LEADENGINE_DB=/data/leads.db
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt
# launch_check.py is imported by /admin/readiness and backup_db.py is the Day-1 backup tool: both are
# runtime files, and leaving them out turns the readiness page into a 500 (found by running this image).
COPY app.py build_partner_kit.py launch_check.py backup_db.py ./
COPY engine engine
COPY config config
COPY static static
COPY templates templates
RUN mkdir -p /data outbox
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 30 --access-logfile -
