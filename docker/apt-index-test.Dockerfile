FROM debian:12-slim

ARG APT_PROXY=
ENV DEBIAN_FRONTEND=noninteractive

RUN if [ -n "$APT_PROXY" ]; then \
      printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$APT_PROXY" "$APT_PROXY" > /etc/apt/apt.conf.d/99proxy; \
    fi

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gpg \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.lyk-ai.com/key.asc \
 | gpg --dearmor -o /usr/share/keyrings/lyk-ai-apt.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/lyk-ai-apt.gpg] https://deb.lyk-ai.com stable main" > /etc/apt/sources.list.d/lyk-ai.list

RUN apt-get update \
 && apt-get install -y --no-install-recommends du-dust lsd \
 && apt-get download bytedance-feishu-stable \
 && test -s bytedance-feishu-stable_*.deb \
 && rm -rf /var/lib/apt/lists/*

