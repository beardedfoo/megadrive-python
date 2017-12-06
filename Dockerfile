FROM python:3.6
COPY . /src
WORKDIR /src
RUN ./run_tests.sh
ENTRYPOINT /src/entrypoint.sh
