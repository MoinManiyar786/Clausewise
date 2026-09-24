FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 TRUST_PROXY_HOPS=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY run.py .
RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8080
CMD ["python", "run.py"]
