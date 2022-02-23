FROM python:3.9.10-alpine
WORKDIR /workdir

COPY get_buddies.py .
COPY requirements.txt .
RUN pip install -r requirements.txt

ENTRYPOINT ["python", "-u", "/workdir/get_buddies.py"]
