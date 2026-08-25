# syntax=docker/dockerfile:1.7

FROM ubuntu:24.04 AS claims-base

ARG DEBIAN_FRONTEND=noninteractive
ARG HERMES_INSTALL_URL=https://hermes-agent.nousresearch.com/install.sh
ARG HERMES_COMMIT=""
ARG CLAIMS_REVISION="unknown"

LABEL org.opencontainers.image.source="https://github.com/DeSciClaims/Claims" \
      org.opencontainers.image.revision="${CLAIMS_REVISION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        ghostscript \
        git \
        libgl1 \
        libglib2.0-0 \
        nano \
        openssh-client \
        openssh-server \
        poppler-utils \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        tini \
        tmux \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/claims/.venv \
    PATH=/opt/claims/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/claims \
    PYTHONUNBUFFERED=1 \
    CLAIMS_IMAGE_REVISION=${CLAIMS_REVISION} \
    CLAIMS_DATA_ROOT=/data \
    HERMES_HOME=/data/hermes \
    HF_HOME=/data/cache/huggingface \
    DSPY_CACHEDIR=/data/cache/dspy \
    XDG_CACHE_HOME=/data/cache/xdg \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/claims

RUN python3 -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt

COPY miner ./miner
COPY neurons ./neurons
COPY validator ./validator
COPY schemas ./schemas
COPY examples ./examples
RUN pip install --no-cache-dir --no-deps . \
    && python -c 'import bittensor, dspy, pdf_inspector; print("Claims runtime OK")'

RUN curl -fsSL "${HERMES_INSTALL_URL}" -o /tmp/hermes-install.sh \
    && if [ -n "${HERMES_COMMIT}" ]; then \
         bash /tmp/hermes-install.sh --skip-setup --skip-browser --hermes-home /opt/hermes-seed --commit "${HERMES_COMMIT}"; \
       else \
         bash /tmp/hermes-install.sh --skip-setup --skip-browser --hermes-home /opt/hermes-seed; \
       fi \
    && rm -f /tmp/hermes-install.sh \
    && hermes --help >/dev/null

COPY docker/entrypoint.sh /usr/local/bin/claims-container-entrypoint
RUN chmod 0755 /usr/local/bin/claims-container-entrypoint \
    && ln -s /usr/local/bin/claims-container-entrypoint /usr/local/bin/claims-node

VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/claims-container-entrypoint"]

FROM claims-base AS miner
ENV CLAIMS_NODE_ROLE=miner
CMD ["idle"]

FROM claims-base AS validator-reference-builder
COPY --from=claims_reference_miner . /tmp/claims-reference-miner
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /tmp/reference-wheel /tmp/claims-reference-miner

FROM claims-base AS validator
COPY --from=validator-reference-builder /tmp/reference-wheel /tmp/reference-wheel
RUN pip install --no-cache-dir /tmp/reference-wheel/*.whl \
    && rm -rf /tmp/reference-wheel \
    && python -c 'import claims_reference_miner; print("Reference miner runtime OK")'
ENV CLAIMS_NODE_ROLE=validator \
    CLAIMS_REFERENCE_MINER_CLAIMS_REPO=/opt/claims
CMD ["idle"]
