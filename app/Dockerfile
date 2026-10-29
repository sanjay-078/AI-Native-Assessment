FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -r requirements.txt
COPY app ./app
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8090"]
