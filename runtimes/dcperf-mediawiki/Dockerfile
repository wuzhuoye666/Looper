# syntax=docker/dockerfile:1.7
ARG APPLIANCE_IMAGE=mcr.microsoft.com/devcontainers/base@sha256:81380e4c9c14e8a629ff39029639e4b7893e67400246fa7782a0fe7dc193a02a
FROM ${APPLIANCE_IMAGE}
LABEL org.opencontainers.image.title="Looper DCPerf MediaWiki source appliance" \
      org.opencontainers.image.source="https://github.com/facebookresearch/DCPerf" \
      org.opencontainers.image.revision="9308c3e3c404e0466f0a2929f15ddcf62b2215f6" \
      org.opencontainers.image.licenses="MIT" \
      dev.looper.runtime.contract="dcperf-mediawiki-source-appliance-v1"
USER root
COPY source.tar.gz /tmp/source.tar.gz
RUN mkdir -p /opt/dcperf \
    && test "$(sha256sum /tmp/source.tar.gz | cut -d' ' -f1)" = "acdc2ad0b0f03adf44bf6803abe9e7880be7c61847c7caa51c41d55a96a62371" \
    && tar -xzf /tmp/source.tar.gz --strip-components=1 -C /opt/dcperf \
    && rm /tmp/source.tar.gz \
    && python3 -m compileall -q /opt/dcperf/benchpress
COPY looper-dcperf-mediawiki /usr/local/bin/looper-dcperf-mediawiki
RUN chmod 0555 /usr/local/bin/looper-dcperf-mediawiki
ENV PYTHONPATH=/opt/dcperf HOME=/tmp
WORKDIR /opt/dcperf
USER vscode
CMD ["looper-dcperf-mediawiki", "--self-check"]
