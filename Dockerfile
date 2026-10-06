FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app
COPY src/ ./src/
COPY scripts/serve.py scripts/container_start.py ./scripts/
COPY config/ ./config/
COPY web/ ./web/
RUN mkdir -p data outputs/app outputs/phase2 outputs/scenarios && chown -R app:app data outputs
USER app
EXPOSE 8002
CMD ["python", "scripts/container_start.py"]
