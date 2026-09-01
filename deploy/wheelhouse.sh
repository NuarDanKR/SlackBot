#!/usr/bin/env bash
# 인터넷 없는 서버용 — 휠(wheel) 미리 내려받기.
# 인터넷 되는 곳에서 실행하고, 생성된 wheels/ 를 서버로 함께 옮긴다.
#
#   bash deploy/wheelhouse.sh
#   → wheels/ 생성 → 서버에서: sudo OFFLINE=1 bash deploy/install.sh
#
# 주의: 대상은 **리눅스 x86_64 / Python 3.11**. Windows 에서 실행해도
#       --platform 지정 덕분에 리눅스용 휠을 받는다(pydantic_core 등 컴파일 패키지 때문에 필수).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 콘솔까지 오프라인으로 설치하려면 이 목록도 함께 받는다.
#   WITH_CONSOLE=1 bash deploy/wheelhouse.sh
REQS=(-r requirements.txt)
[[ "${WITH_CONSOLE:-0}" == "1" ]] && REQS+=(-r deploy/requirements-console.txt)

python -m pip download "${REQS[@]}" -d wheels \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 3.11 \
  --implementation cp

echo
echo "완료: $(ls wheels | wc -l) 개 휠 → wheels/"
echo "서버로 옮긴 뒤: sudo OFFLINE=1 bash deploy/install.sh"
