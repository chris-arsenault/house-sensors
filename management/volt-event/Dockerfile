FROM nginx:alpine

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
