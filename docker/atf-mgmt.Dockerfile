# atf-mgmt — MGMT toolbox image. Build from the repo root:
#   docker build -f docker/atf-mgmt.Dockerfile -t atf-mgmt:latest .
FROM python:3.12-slim

# OS toolchain (nmap for scans; iputils-ping for ICMP reachability; sslyze/testssl land with E.3).
# Without iputils-ping, the reachability probe (ctx.mgmt.ping) hits FileNotFoundError inside the
# container and reports "ping: down" even when the target answers ICMP. The worker runs as the
# caller's NON-root uid, and hosts commonly set net.ipv4.ping_group_range to an empty range (e.g.
# "1 0"), so a plain ping can't open its ICMP socket ("Operation not permitted"). Grant the ping
# binary the cap_net_raw file capability — with Docker's default NET_RAW bounding set that lets the
# non-root worker send ICMP without needing --cap-add or a permissive ping_group_range.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nmap iputils-ping libcap2-bin ca-certificates \
    && setcap cap_net_raw+ep /usr/bin/ping \
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
