# syntax=docker/dockerfile:1.7
ARG JAVA_IMAGE=ghcr.io/graalvm/jdk-community@sha256:4ae63cc6caa75d0f5d144c20ed4a17760994fd12c8ceb8b7dc6e405f1593ac65
ARG GO_IMAGE=mcr.microsoft.com/oss/go/microsoft/golang@sha256:97e44f3c610d5fd44e0d2bd86bd533b48151488d18cdd53032af0c65a4f7af24

FROM ${GO_IMAGE} AS launcher
WORKDIR /src
COPY launcher/main.go ./main.go
RUN gofmt -w main.go && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/looper-benchbase-smallbank ./main.go

FROM ${JAVA_IMAGE} AS upstream-build
USER root
WORKDIR /src
COPY source.tar.gz /tmp/source.tar.gz
RUN tar -xzf /tmp/source.tar.gz --strip-components=1 -C /src \
    && test "$(sha256sum /tmp/source.tar.gz | cut -d' ' -f1)" = "d640d02bd20c72896fce7dd616824d874e60606d3b16f46587450db076a37843" \
    && chmod +x ./mvnw \
    && ./mvnw -B -P postgres -DskipTests -Ddescriptors=src/main/assembly/tgz.xml package \
    && test -f target/benchbase-postgres.tgz

FROM ${JAVA_IMAGE}
LABEL org.opencontainers.image.title="Looper BenchBase SmallBank runtime" \
      org.opencontainers.image.source="https://github.com/cmu-db/benchbase" \
      org.opencontainers.image.revision="33c00473807ebd49304d114a6d769d2d2b2bbb34" \
      org.opencontainers.image.licenses="Apache-2.0" \
      dev.looper.runtime.contract="benchbase-smallbank-v1"
USER root
COPY --from=upstream-build /src/target/benchbase-postgres.tgz /tmp/benchbase-postgres.tgz
RUN mkdir -p /opt/benchbase \
    && tar -xzf /tmp/benchbase-postgres.tgz --strip-components=1 -C /opt/benchbase \
    && rm /tmp/benchbase-postgres.tgz \
    && test -f /opt/benchbase/benchbase.jar
COPY --from=launcher /out/looper-benchbase-smallbank /usr/local/bin/looper-benchbase-smallbank
ENV HOME=/tmp PATH="/usr/local/bin:${PATH}"
WORKDIR /opt/benchbase
USER 65532:65532
CMD ["looper-benchbase-smallbank", "--self-check"]
