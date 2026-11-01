# good-korean-gpt

Codex가 쓰는 한국어에서 번역문 같은 표현을 걸러 내는 Codex 플러그인입니다. 한국어 규칙 하나와 그 규칙을 지키게 하는 훅 세 개가 들어 있습니다. 규칙은 세션을 시작할 때 모델에게 들어가고, 파일 편집과 셸 명령, MCP 도구 호출, 마지막 답변에 든 한국어는 GPT 6.1 Sol(effort medium)이 규칙과 대조합니다. 규칙에 걸리면 도구 호출을 거부하고, 답변이면 다시 쓰게 합니다.

## good-korean-claude와 다른 점

[good-korean-claude](https://github.com/115dkk/good-korean-claude)는 Claude Code 플러그인입니다. good-korean-gpt는 다음이 다릅니다.

- GPT에 맞춘 규칙이 따로 있습니다. 상대 말을 채점하는 표현, 단정을 피하는 말, 물리적 뜻이 먼저 떠오르는 동사와 형용사를 주로 다룹니다. 원문은 [rules/korean.md](rules/korean.md)에 있습니다.
- 규칙을 부르지 않아도 늘 적용합니다. 세션이 시작될 때마다 규칙 원문이 모델의 맥락에 들어갑니다.
- 마지막 답변도 모델이 규칙과 대조합니다. good-korean-claude는 답변에서 em-dash와 영어 비율만 셉니다.
- 파일에 쓰는 글뿐 아니라 셸 명령(커밋 메시지, PR 본문 등)과 MCP 도구 인자에 든 한국어도 판정합니다.
- 판정은 사용자의 Codex로 GPT 6.1 Sol을 불러 합니다.

## 요구 사항

- Codex CLI 0.166.0 이상. 판정 훅은 PATH의 `codex`로 `codex exec`를 부르는데, 그보다 낮은 버전(0.153.4에서 확인)은 ChatGPT 계정으로 `gpt-6.1-sol`을 부르지 못합니다. 데스크톱 앱만 쓰고 있어 PATH에 `codex`가 없으면 `npm i -g @openai/codex`로 설치하거나, 아래 환경 변수 `GOOD_KOREAN_GPT_CODEX`에 실행 파일 경로를 적으십시오.
- ChatGPT 계정으로 로그인한 Codex. 판정에 드는 사용량은 그 계정의 Codex 사용량에서 나갑니다.
- Python 3.14를 권장하고, 3.10 이상이면 돌아갑니다. macOS와 Linux에서는 `python3`로, Windows에서는 Python 런처 `py -3`로 훅을 실행합니다.

## 설치

먼저 마켓플레이스를 추가하십시오.

```bash
codex plugin marketplace add 115dkk/good-korean-gpt
```

그다음 플러그인을 설치하십시오. Codex 안에서 `/plugins`로 설치해도 됩니다.

```bash
codex plugin add good-korean-gpt@korean
```

플러그인을 설치하거나 켜도 훅은 저절로 신뢰되지 않습니다. 새 세션을 시작하고 `/hooks`에서 good-korean-gpt의 훅 세 개를 검토한 뒤 신뢰해야 훅이 돌기 시작합니다. 업데이트로 훅 정의가 바뀌면 다시 신뢰해야 합니다.

업데이트하려면 다음 명령을 실행하십시오.

```bash
codex plugin marketplace upgrade korean
```

## 들어 있는 것

### 규칙 `rules/korean.md`

존댓말, 번역문 같은 완충 표현, 채점하는 말투, 습관적인 비유와 추상어, 물리적 뜻이 먼저 떠오르는 동사와 형용사, 단정을 피하는 말, 피동, 쌍점, em-dash를 다룹니다. 세 훅 모두 이 파일 하나를 읽습니다.

### 세션 시작 훅 (SessionStart)

세션을 시작하거나 이어 갈 때와 맥락을 압축한 뒤에 규칙 원문을 모델의 맥락에 넣습니다. 모델을 부르지 않으므로 바로 끝납니다. Codex가 처음부터 규칙대로 쓰면 아래 두 훅이 거부할 일도 줄어듭니다.

### 도구 호출 훅 (PreToolUse)

apply_patch(파일 편집), 셸 명령, MCP 도구가 실행되기 직전에 돌아갑니다. 판정 모델에게 보내는 글은 도구마다 다음과 같습니다.

- apply_patch: 패치가 새로 들이는 줄 가운데 한글이 든 줄과 그 앞뒤 세 줄 안의 새 줄
- 셸 명령: 한글이 든 줄과 그 앞뒤 세 줄
- MCP 도구: 한글이 든 문자열 인자

규칙에 분명히 걸리는 표현이 있으면 도구 호출을 거부합니다. 거부 사유에는 걸린 표현, 어긴 규칙, 고친 말이 들어 있고, Codex는 그것을 보고 고쳐 쓴 뒤 같은 작업을 다시 합니다. 한글과 em-dash가 함께 든 줄은 판정 모델의 답과 상관없이 거부합니다.

한글이 없는 호출, `~/.codex`와 `~/.claude`와 `~/.agents` 아래 파일, `.git` 안의 파일, 잠금 파일은 모델을 부르지 않고 바로 통과합니다. 판정 모델은 식별자, 경로, 검색 패턴, 다른 곳에서 그대로 옮긴 인용은 판정하지 않습니다. 존댓말 규칙은 답변과 사람에게 보내는 메시지에만 적용하므로, 문서와 주석과 커밋 메시지는 '-다'로 끝나는 문체로 써도 됩니다.

### 답변 훅 (Stop)

Codex가 턴을 끝내려 할 때 마지막 답변에 한글이 있으면 코드 블록을 뺀 나머지를 판정합니다. 규칙에 걸리면 턴을 끝내지 못하게 하고, 걸린 표현과 고친 말을 주어 답변 전체를 다시 쓰게 합니다. 다시 쓴 답변도 같은 식으로 판정하고, 같은 턴에서 세 번 막아도 고쳐지지 않으면 그 답변은 통과시킵니다. 인용(`>`) 줄과 인라인 코드 안의 em-dash는 세지 않습니다.

걸린 답변은 이미 화면에 나온 뒤이므로 지워지지 않고, 고친 답변이 그 뒤에 이어집니다. Stop 훅은 마지막 답변만 받으므로 작업 도중에 쓰는 진행 메시지는 판정하지 않습니다.

### 판정 모델을 부르지 못할 때

PATH에 `codex`가 없거나, 판정이 600초 안에 끝나지 않거나, 판정 모델이 오류를 돌려주면 막지 않고 통과시킵니다. 통과시킨 사실과 사유는 Codex 화면에 경고로 뜹니다. em-dash는 모델 없이 찾으므로 이때도 막습니다.

## 시간과 사용량

판정할 때마다 `codex exec`를 한 번 띄웁니다. 짧은 글은 10초 남짓 걸리지만, GPT 6.1 Sol 서버가 느릴 때는 몇 분이 걸리기도 합니다. 그래서 판정 한도를 600초, 훅 한도를 660초로 잡았습니다. 판정 한 번에 Codex의 기본 지침까지 들어가 토큰을 2만 개 가까이 씁니다.

## 환경 변수

Codex를 실행하는 환경에 설정하십시오.

| 이름 | 뜻 | 기본값 |
|------|----|--------|
| `GOOD_KOREAN_GPT_CODEX` | 판정에 쓸 codex 실행 파일의 경로나 이름 | PATH의 `codex` |
| `GOOD_KOREAN_GPT_MODEL` | 판정 모델 | `gpt-6.1-sol` |
| `GOOD_KOREAN_GPT_EFFORT` | 판정 effort | `medium` |

## 개발

훅 스크립트는 Ruff로 검사합니다. 설정은 [ruff.toml](ruff.toml)에 있고, GitHub Actions가 push와 PR마다 `ruff check`와 `ruff format --check`를 돌립니다. 로컬에서 같은 검사를 하려면 다음 두 명령을 실행하십시오.

```bash
ruff check .
```

```bash
ruff format --check .
```

`.codex-plugin/plugin.json`의 `version`은 main에 push가 들어올 때마다 GitHub Actions가 맞춰 올리므로 손으로 고치지 않습니다.

## 라이선스

MIT입니다. [LICENSE](LICENSE)를 보십시오.
