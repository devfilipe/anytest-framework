# atf-mgmt — MGMT toolbox image. Build from the repo root:
#   docker build -f docker/atf-mgmt.Dockerfile -t atf-mgmt:latest .
FROM python:3.12-slim

# OS toolchain (nmap for scans; sslyze/testssl land with E.3).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nmap ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY atf ./atf
RUN pip install --no-cache-dir .[mgmt]

ENV ATF_IMAGE=atf-mgmt
# Check code is NOT baked in — it is mounted at run time from the check-source repos and found
# via $ATF_CHECK_SOURCES, so editing a mgmt check needs no image rebuild. The runner invokes:
#   docker run --rm --network host -v <out>:/out -v <repo>:/checks/<n> -e ATF_CHECK_SOURCES=… \
#     atf-mgmt atf _mgmt-worker --out /out
