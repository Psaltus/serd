FROM nginx:alpine

# Default keeps docker compose working unchanged; a deployment where both
# containers share one network namespace overrides it with 127.0.0.1.
ENV BACKEND_HOST=backend
# Restricts envsubst to this one variable, so nginx's own $host, $remote_addr
# and friends survive the templating step intact.
ENV NGINX_ENVSUBST_FILTER=BACKEND_HOST

# The only page nginx serves itself, and only when the backend cannot be
# reached to render one. Everything else comes from the backend.
COPY nginx/fallback.html /usr/share/nginx/fallback/fallback.html
COPY nginx/proxy_common.conf /etc/nginx/snippets/proxy_common.conf
COPY nginx/nginx.conf /etc/nginx/templates/default.conf.template
