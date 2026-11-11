#!/usr/bin/env bash
# deputy-up.sh - 출근 전 무인 운영 기동
#
# 사용법:  ./deputy-up.sh [프로젝트경로]
# 하는 일: 점검 → 절전 차단 → 멤버별 백그라운드 세션 기동(목표 부착)
set -euo pipefail

# Windows(cp949 등) 로케일에서 python3 -c 의 stdout 이 파이프로 캡처될 때
# UTF-8 이 아니라 로케일 코드페이지로 인코딩되어, 한글 brief/owns 가 깨진
# 바이트로 $PROMPT 에 섞여 들어간다. 이 깨진 텍스트가 claude --bg 의 인자로
# 넘어가면 조용히 멈춰버린다(재현: bash -x 로 확인, 180초 타임아웃 후 죽음).
export PYTHONIOENCODING=utf-8

ONLY=""
PROJECT="$PWD"
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift 2 ;;
    --only=*) ONLY="${1#*=}"; shift ;;
    *) PROJECT="$1"; shift ;;
  esac
done
cd "$PROJECT"

# PATH 에 설치돼 있으면 그걸 쓰고, 아니면 이 스크립트 옆의 것을 쓴다
if command -v deputy >/dev/null 2>&1; then
  DEPUTY="$(command -v deputy)"
else
  DEPUTY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deputy"
fi
CFG=".deputy/deputy.json"

[ -f "$CFG" ] || { echo "deputy-up: $PROJECT 에 .deputy/deputy.json 이 없습니다. 'deputy init' 을 먼저 실행하세요."; exit 1; }

if [ -n "$ONLY" ]; then
  MEMBERS_OVERRIDE="$ONLY"
  QUIET=1
fi

if [ -z "${QUIET:-}" ]; then
echo "=== 1/4 점검 ==="
if ! "$DEPUTY" doctor; then
  echo
  echo "차단 항목이 있습니다. 고친 뒤 다시 실행하세요."
  echo "(그래도 강행하려면 DEPUTY_FORCE=1 를 붙이세요)"
  [ "${DEPUTY_FORCE:-0}" = "1" ] || exit 1
fi

fi

if [ -z "${QUIET:-}" ]; then
echo
echo "=== 2/4 절전 차단 ==="
if [ "$(uname -s)" != "Darwin" ]; then
  echo "  macOS 가 아니라 건너뜀 (Windows 는 deputy doctor 의 F-01 이 전원 설정을 확인합니다)"
elif pgrep -x caffeinate >/dev/null; then
  echo "  이미 실행 중"
else
  caffeinate -dimsu &
  echo "  caffeinate 시작 (pid $!)"
fi
# deputy stop 이 남긴 멈춤 표시를 지운다. 지우지 않으면 deputy watch 가 되살리지 않는다.
"$DEPUTY" stop --resume >/dev/null 2>&1 || true

fi

[ -z "${QUIET:-}" ] && { echo; echo "=== 3/4 세션 기동 ==="; }
MEMBERS="${MEMBERS_OVERRIDE:-$(python3 -c "import json;print(' '.join(m['name'] for m in json.load(open('$CFG', encoding='utf-8'))['members']))")}"

FAILED=0
for M in $MEMBERS; do
  BRIEF=$(python3 -c "
import json
m=[x for x in json.load(open('$CFG', encoding='utf-8'))['members'] if x['name']=='$M'][0]
print(m.get('brief','') or '(역할 미지정)')")
  OWNS=$(python3 -c "
import json
m=[x for x in json.load(open('$CFG', encoding='utf-8'))['members'] if x['name']=='$M'][0]
print(', '.join(m.get('owns',[])) or '(담당 미지정)')")
  ROLE=$(python3 -c "
import json
m=[x for x in json.load(open('$CFG', encoding='utf-8'))['members'] if x['name']=='$M'][0]
print(m.get('role','builder'))")

  if [ "$ROLE" = "coordinator" ]; then
    read -r -d '' PROMPT <<EOF || true
너는 deputy 조율자 세션 '$M' 이다. $BRIEF
담당 디렉터리가 없다. 코드를 고치지 않는다.

너만 볼 수 있는 것이 있다. 담당자들은 자기 영역만 보지만 너는 전체를 본다.
그래서 아무도 못 잡는 것을 잡는 게 네 일이다.
  · 두 제안이 같은 파일을 건드리려 하는가
  · 순서가 뒤집혔는가. 먼저 해야 할 것이 뒤에 있는가
  · 오래 멈춰 있는 작업이 있는가
  · 무주지를 건드리려는 제안이 있는가
  · 백로그가 비어가는가

작업 방식. 매 턴 이 순서를 지킨다.
1. 먼저 \`deputy next\` 를 실행한다. 무엇을 할지는 이 출력이 정한다.
2. 리뷰가 본업이다. 형식적으로 동의하지 말고 위 항목들을 실제로 확인한 뒤 판단한다.
   담당자가 놓친 전체 관점의 문제를 찾는 것이 네가 존재하는 이유다.
3. 합의가 깨진 이슈(deputy:blocked)는 네가 중재한다. 양쪽 근거를 읽고
   범위를 어떻게 좁힐지 또는 어떻게 쪼갤지 정해 이슈에 남긴다.
4. 급한 게 없으면 백로그를 손본다. 너무 큰 이슈는 쪼개고, 근거가 없는 이슈는 채운다.

금지 사항.
- 코드 파일 수정. 담당 디렉터리가 없다
- deputy claim 으로 작업 착수
- 담당자에게 지시하지 말고 근거를 들어 설득한다. 판단은 담당자가 한다

코드 변경이 필요하면 담당 세션에 메시지로 요청한다:
  "@frontend 이 제안이 backend 와 같은 파일을 건드려. 순서를 바꾸는 게 어때?"

사람은 출근해 있고 폰으로 GitHub 이슈를 본다. 이슈 코멘트가 유일한 보고 채널이다.

시작한다. 지금 바로 \`deputy next\` 를 실행하라.
EOF
  else
    read -r -d '' PROMPT <<EOF || true
너는 deputy 세션 '$M' 이다. 역할: $BRIEF
담당 디렉터리: $OWNS — 이 밖의 파일은 절대 수정하지 않는다.

작업 방식. 매 턴 이 순서를 지킨다.
1. 먼저 \`deputy next\` 를 실행한다. 무엇을 할지는 이 출력이 정한다. 스스로 정하지 않는다.
2. 출력의 지시를 그대로 수행한다. 우선순위가 이미 정렬되어 있다.
3. 리뷰를 요청받으면 형식적으로 동의하지 말고 실제로 코드를 읽고 판단한다.
   근거 없는 AGREE 는 합의를 무의미하게 만든다. 우려가 있으면 OBJECT 나 AMEND 를 쓴다.
4. 제안할 때는 반드시 대안을 하나 이상 비교하고, 완료 기준을 명령과 기대 출력으로 쓴다.
5. 작업 중 문제를 발견하면 판단한다. 부모의 완료 기준을 이것 없이 만족시킬 수 없다면
   \`deputy derive <부모번호>\` 로 파생 이슈를 만든다. 아니면 독립 이슈이거나 그냥 노트다.
   파생 이슈는 자식을 가질 수 없고, 부모당 3개를 넘길 수 없다.
6. 부모와 파생 이슈를 모두 끝낸 뒤에야 다음 최상위 작업으로 넘어간다.
   \`deputy done <번호> --close --result "..."\` 로 기록하고 다시 1번으로 간다.

금지 사항.
- 담당 밖 파일 수정
- main/master 브랜치로 push
- 배포, 마이그레이션, 시크릿 조작
- 이견이 남았는데 deputy claim --force 로 밀어붙이기

막혀도 절대 질문하고 멈추지 않는다. 사람이 없다. 질문은 아무도 못 읽는다.
막히면 \`deputy note <번호> "무엇에 막혔는지"\` 로 이슈에 남기고 곧바로 \`deputy next\` 로 넘어간다.
할 일이 하나도 없어 보여도 멈추지 않는다. 다른 세션의 제안을 리뷰하거나,
담당 영역의 코드를 읽고 다음 작업 후보를 이슈로 등록하거나, 실패하는 테스트를 미리 써둔다.
사람은 출근해 있고 폰으로 GitHub 이슈를 본다. 이슈 코멘트가 유일한 보고 채널이다.

시작한다. 지금 바로 \`deputy next\` 를 실행하라.
EOF
  fi

  # /goal 로 감싼다. 이게 없으면 세션이 한 턴만 돌고 멈춘다.
  # 조건은 대화에 드러난 것만으로 판정 가능해야 한다 - 판정 모델은 도구를 못 쓴다.
  GOAL="/goal deputy next 가 후보 이슈 없음을 보고했고, 내가 착수한 이슈가 모두 닫혔다."
  GOAL="$GOAL 매 턴 반드시 deputy next 를 먼저 실행하고 그 출력의 지시를 그대로 따른다."
  GOAL="$GOAL 무엇을 할지 스스로 정하지 않는다. 담당 디렉터리 밖의 파일을 수정하지 않는다."
  GOAL="$GOAL main 으로 push 하지 않고 배포와 마이그레이션을 하지 않는다."
  GOAL="$GOAL 막히면 deputy note 로 이슈에 남기고 다음 할 일로 넘어간다. ${MAX_TURNS:-60}턴을 넘기면 중단한다."
  PROMPT="$PROMPT

$GOAL"

  echo "  → $M ($ROLE) 기동 중..."
  DEPUTY_MEMBER="$M" claude --bg --name "deputy-$M" "$PROMPT" >/dev/null 2>&1 || {
    FAILED=1
    echo "     실패. 수동으로 실행하세요:"
    echo "     DEPUTY_MEMBER=$M claude --bg --name deputy-$M \"...\""
  }
done

if [ "$FAILED" -ne 0 ]; then
  echo "일부 세션 기동에 실패했습니다. 성공한 세션은 계속 실행됩니다."
  exit 1
fi
if [ -n "${QUIET:-}" ]; then exit 0; fi
echo
echo "=== 4/4 완료 ==="
echo "  세션 관제:  claude agents      (Ctrl+T 로 각 세션을 고정하세요 - F-03)"
echo "  진행 상황:  deputy status  또는 폰에서 GitHub 이슈"
echo "  퇴근 후:    deputy status && gh pr list"
echo
echo "각 세션에 목표를 걸어두려면 agent view 에서 붙어 다음을 입력하세요:"
echo "  /goal deputy next 가 후보 이슈 없음을 보고하고, 내가 착수한 이슈가 모두 닫혔다."
echo "        매 턴 deputy next 를 실행한 기록이 대화에 있어야 한다. 60턴을 넘기면 중단한다."
