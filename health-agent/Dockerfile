FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY health_agent.py .

# Log file lives here inside the container; mount a volume over this
# path (see docker-compose.yml) to persist it on the host.
RUN mkdir -p /var/log/health_agent

CMD ["python", "-u", "health_agent.py"]
