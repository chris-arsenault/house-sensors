# Debian rather than Alpine: aws_signing_helper is published as a glibc binary
# and will not exec against musl, which presents as "not found" and then as a
# container that cannot read its own token.
FROM nginx:bookworm

ARG AWS_SIGNING_HELPER_VERSION=1.8.4
ARG AWS_SIGNING_HELPER_SHA256=b7568acd6e1517a4e1adaee68d52bfd6284a0e5305677166cd83d43a07c815c9

# The enrollment bootstrap is bash, speaks to the trust appliance over curl and
# openssl, and reads this service's InfluxDB token itself rather than receiving
# it from a deploy (ahara-trust ADR-0002). nginx's own entrypoint still runs
# afterwards, so the token reaches the config template the way it always has.
RUN apt-get update && apt-get install -y --no-install-recommends \
    awscli \
    bash \
    ca-certificates \
    curl \
    openssl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL \
    "https://rolesanywhere.amazonaws.com/releases/${AWS_SIGNING_HELPER_VERSION}/X86_64/Linux/Amzn2023/aws_signing_helper" \
    -o /usr/local/bin/aws_signing_helper \
    && echo "${AWS_SIGNING_HELPER_SHA256}  /usr/local/bin/aws_signing_helper" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/aws_signing_helper

COPY truenas-roles-anywhere-bootstrap /usr/local/bin/truenas-roles-anywhere-bootstrap
RUN chmod 0755 /usr/local/bin/truenas-roles-anywhere-bootstrap

# Copy the HTML file
COPY index.html /usr/share/nginx/html/

# Copy nginx configuration template - nginx will process this automatically
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Validate the templated nginx config with placeholder values at build time.
RUN set -eu; \
    cp /etc/nginx/conf.d/default.conf /tmp/default.conf.original; \
    INFLUX_URL=http://127.0.0.1:8086 \
    INFLUX_ORG=ahara \
    INFLUX_BUCKET=voltage-data \
    INFLUX_TOKEN=local-placeholder-token \
    envsubst '${INFLUX_URL} ${INFLUX_ORG} ${INFLUX_BUCKET} ${INFLUX_TOKEN}' \
        < /etc/nginx/templates/default.conf.template \
        > /etc/nginx/conf.d/default.conf; \
    nginx -t; \
    mv /tmp/default.conf.original /etc/nginx/conf.d/default.conf

# Expose port
EXPOSE 80

# Use default nginx entrypoint (it handles templates automatically)
CMD ["nginx", "-g", "daemon off;"]
