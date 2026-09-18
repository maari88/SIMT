FROM python:3.10-alpine
WORKDIR /app
RUN pip install flask redis
COPY app.py .
EXPOSE 8000
CMD ["python", "app.py"]
