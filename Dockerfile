FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY create_platform_admin.py .
COPY recover_platform_admin.py .
COPY backup_database.py .
COPY static ./static
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["python","-m","uvicorn","main:app","--host","0.0.0.0","--port","8000","--proxy-headers"]
