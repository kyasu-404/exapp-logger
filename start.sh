#!/bin/bash
set -e

if [ -n "$HP_SHARED_KEY" ]; then
    echo "HaRP detected, configuring FRP tunnel"
    if [ -d "/certs/frp" ]; then
        cat <<EOF > /frpc.toml
serverAddr = "$HP_FRP_ADDRESS"
serverPort = $HP_FRP_PORT
loginFailExit = false

transport.tls.enable = true
transport.tls.certFile = "/certs/frp/client.crt"
transport.tls.keyFile = "/certs/frp/client.key"
transport.tls.trustedCaFile = "/certs/frp/ca.crt"
transport.tls.serverName = "harp.nc"

metadatas.token = "$HP_SHARED_KEY"

[[proxies]]
remotePort = $APP_PORT
type = "tcp"
name = "$APP_ID"
[proxies.plugin]
type = "unix_domain_socket"
unixPath = "/tmp/exapp.sock"
EOF
    else
        cat <<EOF > /frpc.toml
serverAddr = "$HP_FRP_ADDRESS"
serverPort = $HP_FRP_PORT
loginFailExit = false

transport.tls.enable = false
metadatas.token = "$HP_SHARED_KEY"

[[proxies]]
remotePort = $APP_PORT
type = "tcp"
name = "$APP_ID"
[proxies.plugin]
type = "unix_domain_socket"
unixPath = "/tmp/exapp.sock"
EOF
    fi
    frpc -c /frpc.toml &
fi

exec "$@"

