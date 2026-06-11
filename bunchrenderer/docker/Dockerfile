# Headless Blender image for BunchRenderer.
# Built automatically by `bunchrender` on first use; build manually with:
#   docker build -t bunchrenderer/blender:5.0.1 bunchrenderer/docker
ARG BLENDER_VERSION=5.0.1

FROM ubuntu:22.04
ARG BLENDER_VERSION
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils \
        libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 \
        libgl1 libegl1 libxkbcommon0 libsm6 libice6 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    series="${BLENDER_VERSION%.*}"; \
    url="https://download.blender.org/release/Blender${series}/blender-${BLENDER_VERSION}-linux-x64.tar.xz"; \
    curl -fsSL "$url" -o /tmp/blender.tar.xz; \
    mkdir -p /opt/blender; \
    tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1; \
    rm /tmp/blender.tar.xz

ENV PATH="/opt/blender:${PATH}"

WORKDIR /work
CMD ["blender", "--version"]
