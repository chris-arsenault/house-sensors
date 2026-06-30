#!/bin/sh

# Replace environment variables in nginx config
envsubst '${INFLUX_URL} ${INFLUX_ORG} ${INFLUX_BUCKET} ${INFLUX_TOKEN}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf

# Start nginx
exec "$@"
