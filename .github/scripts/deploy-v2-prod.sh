#!/bin/bash
# V2 prod AI 인스턴스 무중단 배포 (GitHub Actions가 SSM으로 root 권한 실행)
#
# 구조: nginx가 BE의 고정 포트(8000 live, 8001 analysis)를 받고, 뒤의 컨테이너 묶음(슬롯) 하나로 보낸다.
#   blue  슬롯: live 127.0.0.1:8010, analysis 127.0.0.1:8011
#   green 슬롯: live 127.0.0.1:8020, analysis 127.0.0.1:8021
# 인스턴스마다 ROLES에 적힌 컨테이너만 띄우고, nginx도 그 포트만 받는다 (live 인스턴스와 analysis 인스턴스를 나눠 실행).
# 순서: 비어 있는 슬롯에 새 버전 실행 → health 확인 → nginx 전환(reload) → 이전 슬롯은 연결이 끝나면 정리
# nginx reload는 기존 연결(진행 중인 녹음 WebSocket, 분석 요청)을 이전 worker가 끝까지 처리한다.
#
# workflow가 앞에 붙여 주는 값: IMAGE, MODE(real), REGION, BACKEND_BASE_URL, ROLES("live", "analysis", "live analysis")
set -euo pipefail

BASE=/opt/meety
ENV_FILE=$BASE/ai.env
ACTIVE_FILE=$BASE/active-slot
NGINX_CONF=/etc/nginx/conf.d/meety-ai.conf
PARAM_PREFIX=/meety/prod

case "$MODE" in real) ;; *) echo "운영 배포는 real 모드만 허용한다: $MODE"; exit 1 ;; esac
case "$ROLES" in live|analysis|"live analysis") ;; *) echo "ROLES는 live, analysis, \"live analysis\" 중 하나여야 한다: $ROLES"; exit 1 ;; esac

has_role() { [[ " $ROLES " == *" $1 "* ]]; }

slot_ports() { # 슬롯 이름 → "live포트 analysis포트"
  case "$1" in blue) echo "8010 8011" ;; green) echo "8020 8021" ;; esac
}

param() { /snap/bin/aws ssm get-parameter --region "$REGION" --name "$1" --with-decryption --query Parameter.Value --output text; }

# 1. 처음 한 번: nginx 설치. 기본 사이트(80번)는 쓰지 않는다
if ! command -v nginx > /dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q nginx
  rm -f /etc/nginx/sites-enabled/default
fi

# 2. 운영 환경 파일. 공급자 키는 인스턴스 역할로 Parameter Store에서 읽고, root만 읽을 수 있게 저장한다
# 명령 치환을 echo 안에 넣으면 조회 실패가 가려지므로 먼저 확인한다.
INTERNAL_API_KEY=$(param "$PARAM_PREFIX/be/INTERNAL_API_KEY")
[ -n "$INTERNAL_API_KEY" ] || { echo "운영 INTERNAL_API_KEY가 비어 있다"; exit 1; }
umask 077
{
  echo "MEETY_ENVIRONMENT=production"
  echo "MEETY_LOG_LEVEL=INFO"
  echo "TZ=Asia/Seoul"
  echo "MEETY_BACKEND_BASE_URL=$BACKEND_BASE_URL"
  echo "INTERNAL_API_KEY=$INTERNAL_API_KEY"
  echo "MEETY_MAX_RECORDING_CONNECTIONS=100"
  /snap/bin/aws ssm get-parameters-by-path --region "$REGION" --path "$PARAM_PREFIX/ai" \
    --with-decryption --query 'Parameters[].[Name,Value]' --output text \
    | awk -F'\t' '{ n = split($1, p, "/"); print p[n] "=" $2 }'
  echo "LANGSMITH_TRACING=true"
} > "$ENV_FILE.new"
umask 022
for key in SPEECHMATICS_API_KEY OPENROUTER_API_KEY MODAL_TOKEN_ID MODAL_TOKEN_SECRET; do
  grep -q "^$key=." "$ENV_FILE.new" || { echo "Parameter Store에 $key가 없다 ($PARAM_PREFIX/ai)"; rm -f "$ENV_FILE.new"; exit 1; }
done
LIVE_APP=meety_ai.live:create_live_app
ANALYSIS_APP=meety_ai.analysis:create_analysis_app
mv "$ENV_FILE.new" "$ENV_FILE"

# 3. 새 슬롯 결정. 처음 배포면 blue
ACTIVE=$(cat "$ACTIVE_FILE" 2>/dev/null || true)
if [ "$ACTIVE" = blue ]; then NEW=green; else NEW=blue; fi
read -r LIVE_PORT ANALYSIS_PORT <<< "$(slot_ports "$NEW")"
echo "모드: $MODE, 실행할 컨테이너: $ROLES, 사용 중인 슬롯: ${ACTIVE:-없음}, 새 슬롯: $NEW"

# 새 슬롯 자리에 남아 있는 예전 컨테이너(두 번 전 배포)와 정리 예약을 지운다
systemctl stop "meety-ai-cleanup-$NEW" 2> /dev/null || true
docker rm -f "ai-live-$NEW" "ai-analysis-$NEW" 2> /dev/null || true

# 4. 새 슬롯 실행
docker pull -q "$IMAGE"
run_container() { # 이름 호스트포트 컨테이너포트 앱
  docker run -d --name "$1" --restart unless-stopped \
    -p "127.0.0.1:$2:$3" --env-file "$ENV_FILE" \
    --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
    "$IMAGE" uvicorn "$4" --factory --host 0.0.0.0 --port "$3" > /dev/null
}
HEALTH_PORTS=()
if has_role live; then
  run_container "ai-live-$NEW" "$LIVE_PORT" 8000 "$LIVE_APP"
  HEALTH_PORTS+=("$LIVE_PORT")
fi
if has_role analysis; then
  run_container "ai-analysis-$NEW" "$ANALYSIS_PORT" 8001 "$ANALYSIS_APP"
  HEALTH_PORTS+=("$ANALYSIS_PORT")
fi

# 5. 새 슬롯 health 확인. 실패하면 새 슬롯만 지우고 끝낸다. 기존 슬롯은 계속 서비스한다
slot_healthy() {
  local port
  for port in "${HEALTH_PORTS[@]}"; do
    curl -fsS -o /dev/null "http://127.0.0.1:$port/healthz" || return 1
  done
}
healthy=false
for i in $(seq 1 60); do
  if slot_healthy; then
    healthy=true
    break
  fi
  sleep 2
done
if [ "$healthy" != true ]; then
  echo "새 슬롯이 응답하지 않아 제거한다"
  docker logs --tail 30 "ai-live-$NEW" 2>&1 || true
  docker logs --tail 30 "ai-analysis-$NEW" 2>&1 || true
  docker rm -f "ai-live-$NEW" "ai-analysis-$NEW" 2> /dev/null || true
  exit 1
fi
echo "새 슬롯 정상: $NEW"

# 6. nginx 전환. 녹음 WebSocket과 긴 분석 요청이 끊기지 않도록 시간 제한을 넉넉히 둔다.
#    이 인스턴스에서 띄운 컨테이너의 포트만 받는다
cat > "$NGINX_CONF.new" << EOF
# deploy-v2-prod.sh가 관리한다. 사용 중인 슬롯: $NEW, 컨테이너: $ROLES
map \$http_upgrade \$connection_upgrade {
  default upgrade;
  ''      close;
}
EOF

if has_role live; then
  cat >> "$NGINX_CONF.new" << EOF

server {
  listen 8000;
  location / {
    proxy_pass http://127.0.0.1:$LIVE_PORT;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$connection_upgrade;
    proxy_set_header Host \$host;
    proxy_read_timeout 3h;
    proxy_send_timeout 3h;
  }
}
EOF
fi

if has_role analysis; then
  cat >> "$NGINX_CONF.new" << EOF

server {
  listen 8001;
  client_max_body_size 50m;
  location / {
    proxy_pass http://127.0.0.1:$ANALYSIS_PORT;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_read_timeout 3h;
    proxy_send_timeout 3h;
  }
}
EOF
fi
cp -f "$NGINX_CONF" "$NGINX_CONF.prev" 2> /dev/null || true
mv "$NGINX_CONF.new" "$NGINX_CONF"
if ! nginx -t; then
  echo "nginx 설정 오류. 이전 설정으로 되돌리고 새 슬롯을 지운다"
  if [ -f "$NGINX_CONF.prev" ]; then mv "$NGINX_CONF.prev" "$NGINX_CONF"; else rm -f "$NGINX_CONF"; fi
  docker rm -f "ai-live-$NEW" "ai-analysis-$NEW" 2> /dev/null || true
  exit 1
fi
systemctl enable -q nginx
if systemctl is-active -q nginx; then systemctl reload nginx; else systemctl start nginx; fi
echo "$NEW" > "$ACTIVE_FILE"

# 7. nginx를 거쳐 새 슬롯이 응답하는지 확인
if has_role live; then curl -fsS -o /dev/null http://127.0.0.1:8000/healthz; fi
if has_role analysis; then curl -fsS -o /dev/null http://127.0.0.1:8001/healthz; fi
echo "nginx 전환 완료: $NEW"

# 8. 이전 슬롯은 연결이 모두 끝나면 정리한다 (최대 3시간). 배포 명령이 끝나도 계속 돌도록 systemd로 실행한다
if [ -n "$ACTIVE" ]; then
  read -r OLD_LIVE OLD_ANALYSIS <<< "$(slot_ports "$ACTIVE")"
  systemctl stop "meety-ai-cleanup-$ACTIVE" 2> /dev/null || true
  systemctl reset-failed "meety-ai-cleanup-$ACTIVE" 2> /dev/null || true
  systemd-run --quiet --unit "meety-ai-cleanup-$ACTIVE" /bin/bash -c "
    for i in \$(seq 1 180); do
      n=\$(ss -Htn state established '( sport = :$OLD_LIVE or sport = :$OLD_ANALYSIS )' | wc -l)
      [ \"\$n\" -eq 0 ] && break
      sleep 60
    done
    docker rm -f ai-live-$ACTIVE ai-analysis-$ACTIVE"
  echo "이전 슬롯 $ACTIVE: 연결이 끝나면 정리 예약"
fi
docker image prune -f > /dev/null
free -m | head -2
