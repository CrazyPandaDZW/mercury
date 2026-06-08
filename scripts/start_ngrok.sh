#!/bin/bash
# 清除代理后启动 ngrok
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY no_proxy NO_PROXY
exec /opt/homebrew/bin/ngrok http 8080 --log=stdout "$@"
