FROM python:3.12-slim
WORKDIR /kit
COPY mock_gateway.py /kit/mock_gateway.py
EXPOSE 8091
CMD ["python", "mock_gateway.py", "--data", "/assessment-data", "--db", "/state/kit.sqlite"]
