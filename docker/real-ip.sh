#!/bin/sh
# 容器启动时生成 /etc/nginx/real_ip.conf（nginx 官方镜像会执行 /docker-entrypoint.d/*.sh）。
#
# 决定 nginx 是否采信上一层反代传来的 X-Real-IP，详见 docker/nginx.conf 里的说明：
#   * AUTO_TEAM_REAL_IP_FROM 有值：逗号分隔的 IP / CIDR，原样作为 set_real_ip_from；
#     填 none 表示谁都不信。
#   * 没填：按 AUTO_TEAM_BIND 推断。Web 端口只绑回环（默认）时，能连到本容器的只有宿主机
#     本地进程（经 Docker 网桥网关转发）和同一 compose 网络里的容器，信任私有网段是安全的；
#     宿主机反代用自己看到的真实来源覆盖 X-Real-IP 后，限流就能按访客区分。
#     绑到其他地址时外部访客可能直连，默认谁都不信。
set -eu

out=/etc/nginx/real_ip.conf
from="${AUTO_TEAM_REAL_IP_FROM:-}"

if [ -z "$from" ]; then
  case "${AUTO_TEAM_BIND:-127.0.0.1}" in
    127.*|::1|"[::1]"|localhost)
      from="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
      ;;
    *)
      from="none"
      ;;
  esac
fi

: > "$out"
if [ "$from" = "none" ]; then
  echo "real-ip: not trusting X-Real-IP from any peer" >&2
  exit 0
fi

# 只接受 IP / CIDR 字符，避免把别的 nginx 指令拼进配置。
case "$from" in
  *[!0-9A-Fa-f.:/,\ ]*)
    echo "real-ip: AUTO_TEAM_REAL_IP_FROM must be comma-separated IPs/CIDRs or 'none', got: $from" >&2
    exit 1
    ;;
esac

for net in $(echo "$from" | tr ',' ' '); do
  echo "set_real_ip_from $net;" >> "$out"
done
echo "real_ip_header X-Real-IP;" >> "$out"
echo "real-ip: trusting X-Real-IP from $from" >&2
