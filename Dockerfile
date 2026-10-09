# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS native
ARG SAMBA_VERSION=4.25.0
ARG SAMBA_SHA256=2e2cb7296833b35b8f7a7fb76045e0c57adc0c2cd03264b37df5d58e40f28437
ARG BUILD_JOBS=2
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates pkg-config python3-dev python3-setuptools \
    libgnutls28-dev libjansson-dev libpopt-dev libacl1-dev libattr1-dev libcap-dev \
    libldap2-dev libpam0g-dev liblmdb-dev libtirpc-dev liburing-dev libdbus-1-dev \
    libicu-dev libbsd-dev libreadline-dev libnfs-dev flex bison libparse-yapp-perl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL "https://download.samba.org/pub/samba/stable/samba-${SAMBA_VERSION}.tar.gz" -o samba.tar.gz \
    && echo "${SAMBA_SHA256}  samba.tar.gz" | sha256sum -c - \
    && tar xzf samba.tar.gz && mv "samba-${SAMBA_VERSION}" samba
COPY native/ /build/module/
RUN python3 module/register_module.py /build/samba \
    && cd samba && ./configure --prefix=/opt/samba --without-ad-dc --without-ads \
       --without-ldap --disable-python --without-pam --without-systemd --without-libarchive \
    && make -j${BUILD_JOBS} && make install
WORKDIR /app
COPY requirements.lock pyproject.toml ./
COPY cammon/ cammon/
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir setuptools==84.0.0 -r requirements.lock \
    && /opt/venv/bin/python cammon/nfs_build.py

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates passwd libnfs13 libgnutls30 libjansson4 libpopt0 libacl1 libattr1 \
    libcap2 libldap-2.5-0 liblmdb0 libtirpc3 liburing2 libdbus-1-3 libicu72 libbsd0 libreadline8 \
    libgssapi-krb5-2 libcrypt1 \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/opt/venv/bin:/opt/samba/bin:/opt/samba/sbin:$PATH" \
    PYTHONUNBUFFERED=1 CAMMON_DATA_DIR=/run/cammon/runtime CAMMON_CACHE_DIR=/cache
WORKDIR /app
COPY --from=native /opt/samba/ /opt/samba/
COPY --from=native /opt/venv/ /opt/venv/
COPY --from=native /app/cammon/ cammon/
COPY --from=frontend /ui/dist/ frontend/dist/
COPY scripts/ scripts/
EXPOSE 18080 139 445 137/udp 138/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python scripts/healthcheck.py
CMD ["python", "-m", "cammon.main"]

FROM runtime AS test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    nfs-ganesha nfs-ganesha-mem rpcbind libnfs-dev gcc postgresql \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
COPY tests/ tests/
COPY native/ native/
COPY requirements-test.lock ./
RUN pip install --no-cache-dir -r requirements-test.lock
ENV CAMMON_TEST_SAMBA_PREFIX=/opt/samba CAMMON_TEST_NFS=1 CAMMON_TEST_POSTGRES=1
HEALTHCHECK NONE
CMD ["python", "-m", "pytest", "-q"]

# The default build produces the deployable image; integration tests use --target test.
FROM runtime AS production
