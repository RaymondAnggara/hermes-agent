# Egress allowlist proxy for the locked Hermes network (Phase 4 — network default-deny).
#
# Minimal, auditable image: alpine + tinyproxy + our two config files. The proxy
# is the ONLY route off the internal network; tinyproxy runs FilterDefaultDeny On,
# so a destination host is reachable ONLY if it matches a line in allowlist.filter.
#
# Pinned to a digest-able tag; bump deliberately. No app code, no secrets baked in.
FROM alpine:3.20

RUN apk add --no-cache tinyproxy curl ca-certificates \
    && rm -rf /var/cache/apk/*

COPY tinyproxy.conf    /etc/tinyproxy/tinyproxy.conf
COPY allowlist.filter  /etc/tinyproxy/allowlist.filter

# Loopback healthcheck: the proxy answers on 8888. (Does NOT prove egress — the
# validate_egress.sh harness proves allow/deny behaviour from a client container.)
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD curl -fsS -x http://127.0.0.1:8888 http://host.docker.internal:8787/healthz || exit 1

EXPOSE 8888
# tinyproxy in the foreground (-d) so it is PID 1 and docker manages its lifecycle.
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
