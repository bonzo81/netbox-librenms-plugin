ARG FROM
FROM ${FROM}

ARG WHL_FILE
COPY ${WHL_FILE} /opt/netbox/dist/
# The optional host CA lets the install pass a TLS-intercepting proxy: uv reads SSL_CERT_FILE, pip reads PIP_CERT.
RUN --mount=type=secret,id=host_ca,required=false \
    if [ -f "/run/secrets/host_ca" ]; then \
        export SSL_CERT_FILE=/run/secrets/host_ca PIP_CERT=/run/secrets/host_ca; \
    fi; \
    if [ -f "/usr/local/bin/uv" ]; then \
        /usr/local/bin/uv pip install --python /opt/netbox/venv/bin/python /opt/netbox/dist/${WHL_FILE}; \
    else \
        /opt/netbox/venv/bin/pip install /opt/netbox/dist/${WHL_FILE}; \
    fi

# NetBox serves the plugin's static files only after collectstatic sees the plugin enabled.
COPY plugins.py /etc/netbox/config/plugins.py
RUN SECRET_KEY="collectstatic-only-secret-key-0123456789-abcdefghijklmnop" \
    /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py collectstatic --no-input
