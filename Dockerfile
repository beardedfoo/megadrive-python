FROM python:3.6
COPY . /src
ENTRYPOINT /src/entrypoint.sh
