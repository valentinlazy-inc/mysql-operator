# Copyright (c) 2021, 2023 Oracle and/or its affiliates.
#
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
#

FROM container-registry.oracle.com/os/oraclelinux:8 AS pip-stage

ARG PYTHON_TARBALL
ARG PYTHON_ROOT
ARG PYTHON_BASE_DIR

RUN dnf install -y gcc git tar perl-Env libffi-devel \
                   openssl openssl-devel tk-devel xz-devel \
                   zlib-devel bzip2-devel readline-devel \
                   libuuid-devel ncurses-devel make
COPY ${PYTHON_TARBALL} .
RUN mkdir -p  ${PYTHON_BASE_DIR} && cd  ${PYTHON_BASE_DIR} && tar xzf /${PYTHON_TARBALL}
RUN cd ${PYTHON_BASE_DIR}/${PYTHON_ROOT} \
    && ./configure \
    && make \
    && make altinstall \
    && ln -s /usr/local/bin/python3.9 /usr/local/bin/python3 \
    && ln -s /usr/local/bin/pip3.9 /usr/local/bin/pip3

COPY requirements.txt .

RUN pip3 install --target=/tmp/site-packages -r requirements.txt

FROM container-registry.oracle.com/os/oraclelinux:8-slim

COPY --from=pip-stage /tmp/site-packages /usr/lib/mysqlsh/python-packages


