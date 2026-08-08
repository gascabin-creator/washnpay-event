FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
WORKDIR /app
COPY server/requirements.txt .
RUN pip install -r requirements.txt
RUN pip install gunicorn
RUN playwright install chromium --with-deps
COPY server/server.py server/point_auto.py index.html ./
ENV PORT=8080
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 0 server:app
