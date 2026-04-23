FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.1.0

ENV DISPLAY_NUM=1
ENV COMPUTER_HEIGHT_PX=768
ENV COMPUTER_WIDTH_PX=1024

ENV SKIP_BLEATER_BOOT=1
ENV ALLOWED_NAMESPACES="glitchtip,dex,ldap"

# GlitchTip is pre-installed in the Nebula image. This task adds a small
# Dex + LDAP identity path and a Redis-backed replay cache used by setup/grader.
RUN mkdir -p /var/lib/rancher/k3s/agent/images && \
    apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      -o Dpkg::Options::="--force-confold" \
      skopeo && \
    skopeo copy --override-os linux --override-arch amd64 \
      docker://docker.io/dexidp/dex:v2.41.1 \
      docker-archive:/var/lib/rancher/k3s/agent/images/dex.tar:docker.io/dexidp/dex:v2.41.1 && \
    skopeo copy --override-os linux --override-arch amd64 \
      docker://docker.io/bitnamilegacy/openldap:2.6.10-debian-12-r4 \
      docker-archive:/var/lib/rancher/k3s/agent/images/openldap.tar:docker.io/bitnamilegacy/openldap:2.6.10-debian-12-r4 && \
    skopeo copy --override-os linux --override-arch amd64 \
      docker://docker.io/redis:7-alpine \
      docker-archive:/var/lib/rancher/k3s/agent/images/redis.tar:docker.io/redis:7-alpine && \
    skopeo copy --override-os linux --override-arch amd64 \
      docker://docker.io/curlimages/curl:8.7.1 \
      docker-archive:/var/lib/rancher/k3s/agent/images/curl.tar:docker.io/curlimages/curl:8.7.1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
