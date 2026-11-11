FROM python:3.12-slim
WORKDIR /app
ENV AURORA_AI_ALLOW_NETWORK=0
RUN pip install --no-cache-dir "psycopg[binary]==3.2.13"
COPY . .
USER 65534:65534
EXPOSE 8000
CMD ["python","public_demo_cloud.py"]
