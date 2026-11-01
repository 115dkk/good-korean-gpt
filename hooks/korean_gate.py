"""good-korean-gpt 훅: Codex가 쓰는 한국어를 별도 모델(기본 GPT 6.1 Sol)이 규칙과 대조해 판정한다.

하위 명령
  session  SessionStart 훅. 규칙 원문을 모델의 맥락에 넣는다.
  tool     PreToolUse 훅. apply_patch가 새로 들이는 줄, 셸 명령, MCP 도구 인자에 든 한국어를
           판정하고, 규칙에 걸리면 도구 호출을 거부한다.
  reply    Stop 훅. 사용자가 읽을 마지막 답변을 판정하고, 규칙에 걸리면 답변 전체를 다시
           쓰게 한다. 같은 턴에서 세 번 막아도 고쳐지지 않으면 통과시킨다.

판정은 사용자의 codex CLI로 `codex exec`를 불러 하므로 사용량은 사용자의 Codex 요금제에서
나간다. 그 호출은 사용자 설정을 읽지 않고 훅을 끈 채로 돌기 때문에 이 훅이 다시 불리지
않는다. 판정 모델을 부르지 못하면 막지 않고 통과시키되 그 사실을 systemMessage로 알린다.
em-dash는 모델 없이도 찾을 수 있으므로 판정 모델이 실패해도 막는다.

환경 변수
  GOOD_KOREAN_GPT_CODEX   판정에 쓸 codex 실행 파일. 없으면 PATH에서 찾는다.
  GOOD_KOREAN_GPT_MODEL   판정 모델. 기본값은 gpt-6.1-sol.
  GOOD_KOREAN_GPT_EFFORT  판정 effort. 기본값은 medium.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

MODEL = os.environ.get("GOOD_KOREAN_GPT_MODEL") or "gpt-6.1-sol"
EFFORT = os.environ.get("GOOD_KOREAN_GPT_EFFORT") or "medium"
JUDGE_TIMEOUT = 600
MAX_CHARS = 20000
CONTEXT = 3
MAX_BLOCKS = 3
LABEL = "good-korean-gpt"
GUARD_ENV = "GOOD_KOREAN_GPT_ACTIVE"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(ROOT, "rules", "korean.md")
HOME = os.path.expanduser("~")
SKIPPED_ROOTS = tuple(os.path.join(HOME, name) for name in (".codex", ".claude", ".agents"))
LOCKFILES = {"package-lock.json", "cargo.lock", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock"}
STATE_DIR = os.path.join(
    os.environ.get("PLUGIN_DATA") or os.path.join(tempfile.gettempdir(), LABEL),
    "reply-count",
)

HANGUL = re.compile("[가-힣ㄱ-ㆎᄀ-ᇿ]")
DASHES = re.compile("[—―]")
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update) File: (.+)$")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["allow", "deny"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "quote": {"type": "string"},
                    "rule": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["quote", "rule", "fix"],
            },
        },
    },
    "required": ["verdict", "findings"],
}

KINDS = {
    "patch": (
        "the lines a file edit (apply_patch) adds that contain Hangul, and the other added lines "
        f"within {CONTEXT} lines of them, grouped by file"
    ),
    "shell": (
        "the lines of a shell command the agent is about to run that contain Hangul, and the lines "
        f"within {CONTEXT} lines of them"
    ),
    "tool": "the string arguments that contain Hangul in a tool call the agent is about to make",
    "reply": "the agent's final reply to the user, with fenced code blocks left out",
}

INSTRUCTIONS = """You are a gate that judges Korean text an AI coding agent (Codex) wrote, before it
reaches a file, a command or the user. Do not run commands, read files or use any tool. Answer only
with the JSON that the output schema asks for.

You receive {kind}.

Step 1. Decide what is in scope. In scope: every Korean sentence or phrase the agent itself wrote,
such as replies, documents, READMEs, code comments, user-facing strings, commit messages, pull
request and issue text, and messages to other people. Out of scope: identifiers and keys, file
paths, search patterns, text quoted verbatim from another source (a quotation, a cited title, the
bad examples in a rule document, a test input meant to be wrong), and non-Korean text. If nothing
is in scope, answer verdict "allow" with no findings.

Step 2. In the in-scope text, look for expressions that clearly break a specific rule below. The
rule on 존댓말 governs replies and messages addressed to a person; documents, code comments and
commit messages may use the plain written style that ends in -다. Answer "deny" only for clear
breaches you can name. When you are unsure, answer "allow". Style preferences that no rule states
are not breaches.

Each finding must quote the offending expression exactly as it appears in the given text (an exact
substring, not a paraphrase), name the rule it breaks, and give a corrected wording. Write "rule"
and "fix" in Korean, and follow the rules yourself when you write them.

=== Rules ===
{rules}

=== Text to judge ===
{text}
"""

PREAMBLE = (
    "good-korean-gpt 플러그인이 켜져 있습니다. 한국어로 쓰는 모든 글(사용자에게 하는 답변, "
    "파일에 쓰는 문장, 커밋 메시지, 셸 명령과 도구 인자에 든 문장)은 아래 규칙을 따르십시오. "
    "규칙에 걸리는 한국어가 있으면 훅이 편집과 명령을 거부하고, 답변이면 다시 쓰게 합니다."
)


def read_payload():
    try:
        raw = sys.stdin.buffer.read()
        return json.loads(raw.decode("utf-8", errors="replace")) if raw else None
    except ValueError:
        return None


def emit(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def load_rules():
    try:
        with open(RULES_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def near_lines(lines, added=None):
    """새 줄 가운데 한글이 든 줄과 그 앞뒤 CONTEXT줄 안의 줄을 돌려준다. 빈 줄은 뺀다."""
    if added is None:
        added = [True] * len(lines)
    fresh = [i for i, line in enumerate(lines) if added[i] and line.strip()]
    near = set()
    for i in fresh:
        if HANGUL.search(lines[i]):
            near.update(range(i - CONTEXT, i + CONTEXT + 1))
    return [lines[i] for i in fresh if i in near]


def command_text(tool_input):
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        for key in ("command", "input", "patch"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return " ".join(str(part) for part in value)
    return ""


def patch_files(patch):
    """apply_patch 본문을 파일마다 (경로, 줄 목록, 새 줄인지 여부 목록)으로 나눈다.

    빼는 줄과 문맥 줄도 자리를 차지하게 두어야 앞뒤 CONTEXT줄을 원래 간격대로 센다.
    """
    files = []
    current = None
    for line in patch.splitlines():
        header = PATCH_FILE.match(line)
        if header:
            current = (header.group(1).strip(), [], [])
            files.append(current)
        elif line.startswith("*** Move to: ") or line == "*** End of File":
            continue
        elif line.startswith("*** "):
            current = None
        elif current is not None:
            current[1].append(line[1:])
            current[2].append(line.startswith("+"))
    return files


def skipped_path(path, cwd):
    full = os.path.normcase(os.path.abspath(os.path.join(cwd or os.getcwd(), os.path.expanduser(path))))
    if any(full.startswith(os.path.normcase(root) + os.sep) for root in SKIPPED_ROOTS):
        return True
    parts = {part.lower() for part in re.split(r"[\\/]", path)}
    return ".git" in parts or os.path.basename(path).lower() in LOCKFILES


def strings_in(value, key=""):
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from strings_in(item, f"{key}.{name}" if key else str(name))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings_in(item, f"{key}[{index}]")


def tool_blocks(payload):
    """판정할 글을 (종류, [(제목, 줄 목록)]) 꼴로 돌려준다. 판정할 한국어가 없으면 목록이 빈다."""
    name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if name in ("apply_patch", "Edit", "Write"):
        cwd = str(payload.get("cwd") or "")
        blocks = []
        for path, lines, added in patch_files(command_text(tool_input)):
            picked = near_lines(lines, added)
            if picked and not skipped_path(path, cwd):
                blocks.append((f"File: {path}", picked))
        return "patch", blocks
    if name == "Bash":
        picked = near_lines(command_text(tool_input).splitlines())
        return "shell", [("Shell command", picked)] if picked else []
    blocks = []
    for key, value in strings_in(tool_input):
        picked = near_lines(value.splitlines())
        if picked:
            blocks.append((f"Tool {name}, argument {key or '(value)'}", picked))
    return "tool", blocks


def dash_findings(lines):
    findings = []
    for line in lines:
        match = DASHES.search(line) if HANGUL.search(line) else None
        if match:
            findings.append(
                {
                    "quote": line[max(0, match.start() - 12) : match.end() + 12].strip(),
                    "rule": "em-dash를 사용하지 마라.",
                    "fix": "쉼표나 괄호로 바꾸거나 문장을 나누십시오.",
                }
            )
    return findings


def find_codex():
    configured = os.environ.get("GOOD_KOREAN_GPT_CODEX")
    if configured:
        return configured if os.path.isfile(configured) else shutil.which(configured)
    return shutil.which("codex")


def kill_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()


def run(command, data, cwd):
    """명령을 돌려 (종료 코드, 표준 오류, 실패 사유)를 돌려준다. 시간을 넘기면 자식 프로세스까지 끝낸다."""
    if os.name == "nt":
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    else:
        options = {"start_new_session": True}
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(os.environ, **{GUARD_ENV: "1"}),
            **options,
        )
    except OSError as error:
        return None, "", f"판정 모델을 실행하지 못했습니다: {error}"
    try:
        _, stderr = process.communicate(data, timeout=JUDGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=10)
        return None, "", f"판정이 {JUDGE_TIMEOUT}초 안에 끝나지 않았습니다"
    return process.returncode, stderr.decode("utf-8", errors="replace"), None


def last_error(stderr):
    errors = [line.strip() for line in stderr.splitlines() if line.strip().startswith("ERROR")]
    return (errors[-1] if errors else stderr.strip()[-300:])[:300]


def parse_verdict(answer):
    text = answer.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        verdict = json.loads(text)
    except ValueError:
        return None
    if not isinstance(verdict, dict) or verdict.get("verdict") not in ("allow", "deny"):
        return None
    return verdict


def judge(kind, text):
    """판정 모델을 불러 (판정, 실패 사유) 가운데 하나를 채워 돌려준다."""
    rules = load_rules()
    if not rules:
        return None, f"규칙 원문({RULES_PATH})을 읽지 못했습니다"
    codex = find_codex()
    if not codex:
        return None, "codex 실행 파일을 찾지 못했습니다"
    body = text[:MAX_CHARS] + ("\n(이하 생략)" if len(text) > MAX_CHARS else "")
    prompt = INSTRUCTIONS.replace("{kind}", KINDS[kind]).replace("{rules}", rules).replace("{text}", body)
    with tempfile.TemporaryDirectory(prefix=LABEL + "-", ignore_cleanup_errors=True) as work:
        schema_path = os.path.join(work, "schema.json")
        out_path = os.path.join(work, "verdict.json")
        with open(schema_path, "w", encoding="utf-8") as handle:
            json.dump(SCHEMA, handle)
        command = [
            codex,
            "exec",
            "--model",
            MODEL,
            "-c",
            f"model_reasoning_effort={EFFORT}",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--disable",
            "hooks",
            "--color",
            "never",
            "--cd",
            work,
            "--output-schema",
            schema_path,
            "--output-last-message",
            out_path,
            "-",
        ]
        code, stderr, error = run(command, prompt.encode("utf-8"), work)
        if error:
            return None, error
        try:
            with open(out_path, encoding="utf-8") as handle:
                answer = handle.read()
        except OSError:
            answer = ""
    verdict = parse_verdict(answer)
    if verdict is None:
        return None, f"판정 결과를 읽지 못했습니다(종료 코드 {code}): {last_error(stderr)}"
    return verdict, None


def merge_findings(findings, verdict, text):
    """판정 모델이 낸 지적 가운데 실제 글에 있는 표현을 짚은 것만 목록에 넣는다."""
    seen = {item["quote"] for item in findings}
    for item in verdict.get("findings") or []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if quote and quote in text and quote not in seen:
            seen.add(quote)
            findings.append(
                {"quote": quote, "rule": str(item.get("rule") or "").strip(), "fix": str(item.get("fix") or "").strip()}
            )
    return findings


def report(head, findings, tail):
    lines = [f"[{LABEL}] {head} ({MODEL}, effort {EFFORT})"]
    lines.extend(f"- '{item['quote']}': {item['rule']} → {item['fix']}" for item in findings)
    lines.append(tail)
    return "\n".join(lines)


def on_session(payload):
    rules = load_rules()
    if rules:
        emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": PREAMBLE + "\n\n" + rules}})


def on_tool(payload):
    kind, blocks = tool_blocks(payload)
    if not blocks:
        return
    text = "\n\n".join(title + "\n" + "\n".join(lines) for title, lines in blocks)
    findings = dash_findings([line for _, lines in blocks for line in lines])
    verdict, error = judge(kind, text)
    if error and not findings:
        emit({"systemMessage": f"{LABEL}: 판정하지 못해 이 도구 호출은 판정 없이 통과했습니다. 사유: {error}"})
        return
    if verdict and verdict.get("verdict") == "deny":
        findings = merge_findings(findings, verdict, text)
    if not findings:
        return
    reason = report("이 한국어는 규칙에 걸려 쓰지 못했습니다.", findings, "고쳐 쓴 뒤 같은 작업을 다시 하십시오.")
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def drop_fences(text):
    out, fence = [], None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is None:
            if match:
                fence = match.group(1)
                out.append("[코드 블록]")
                continue
            out.append(line)
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and line.strip() == match.group(1):
            fence = None
    return "\n".join(out)


def counter(session_id, update=None):
    path = os.path.join(STATE_DIR, re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "default") + ".count")
    if update is None:
        try:
            with open(path, encoding="utf-8") as handle:
                return int(handle.read().strip() or 0)
        except (OSError, ValueError):
            return 0
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(update))
    return update


def on_reply(payload):
    session_id = str(payload.get("session_id") or "")
    count = counter(session_id) if payload.get("stop_hook_active") else 0
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or not HANGUL.search(text):
        counter(session_id, 0)
        return
    if count >= MAX_BLOCKS:
        counter(session_id, 0)
        emit({"systemMessage": f"{LABEL}: {MAX_BLOCKS}번 다시 써도 규칙에 걸려 이번 답변은 통과시켰습니다."})
        return
    body = drop_fences(text)
    plain = [line for line in re.sub(r"`[^`\n]*`", " ", body).split("\n") if not re.match(r"^\s{0,3}>", line)]
    findings = dash_findings(plain)
    verdict, error = judge("reply", body)
    if error and not findings:
        counter(session_id, 0)
        emit({"systemMessage": f"{LABEL}: 판정하지 못해 이 답변은 판정 없이 통과했습니다. 사유: {error}"})
        return
    if verdict and verdict.get("verdict") == "deny":
        findings = merge_findings(findings, verdict, body)
    if not findings:
        counter(session_id, 0)
        return
    counter(session_id, count + 1)
    reason = report(
        "방금 쓴 답변이 한국어 규칙에 걸렸습니다.",
        findings,
        "답변 전체를 고쳐 다시 쓰십시오. 사과하거나 이 검사를 언급하지 말고 본문만 쓰십시오.",
    )
    emit({"decision": "block", "reason": reason})


def main(argv):
    if os.environ.get(GUARD_ENV):
        return 0
    handler = {"session": on_session, "tool": on_tool, "reply": on_reply}.get(argv[0] if argv else "")
    payload = read_payload()
    if handler and isinstance(payload, dict):
        handler(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as error:  # 훅이 죽으면 턴이 멈추므로, 예상하지 못한 오류는 알리고 통과시킨다.
        emit({"systemMessage": f"{LABEL}: 훅이 오류로 끝나 판정 없이 통과했습니다. 사유: {error!r}"})
        sys.exit(0)
