"""The keeper: deterministic supervisor decisions plus a local-model scribe.

Measured on qwen2.5:7b before this was written: asked to *choose* the next
action from a raw observation it was right 9 times in 18 (it switched healthy
supervisors and reset sessions for context limits that had not been reached),
and asked to judge a failing log it called it "passed". Asked to *extract* a
blocker, a fix command and progress bullets from the same log it was right
every time. So decisions here are plain rules, and the local model only writes
the short result summaries that make a context reset or provider switch cheap,
plus an advisory failure class where the diagnosis regexes find none.
Every model output has a deterministic fallback; the loop never depends on it.
"""
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from . import diagnosis

DEFAULT_KEEPER_MODEL = "qwen2.5-coder:7b"
OLLAMA_URL = "http://localhost:11434"
REMINDER = ("Keeper reminder: you are the supervisor. Do not edit files, run builds or "
            "write the implementation into dispatch prompts. Describe the change and "
            "dispatch a worker to make it.")
LARGE_CODE_LINES = 40


def code_lines(text):
    """Lines inside fenced code blocks: a supervisor pasting an implementation."""
    total, inside = 0, False
    for line in str(text or "").splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            total += 1
    return total


def skeleton_keys(text, head=6, tail=6):
    """Opening and closing word runs of a line, with per-item decoration removed.

    Templates evade exact-duplicate checks by appending ids, quoting the item, or
    slotting a name into a fixed sentence. The fixed parts that remain (how the
    line starts and ends) are what repeat.
    """
    value = re.sub(r"\b[0-9a-f]{6,}\b", " ", str(text).lower())
    value = re.sub(r"[\"“”'‘’][^\"“”'‘’]{3,}[\"“”'‘’]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    words = re.findall(r"[a-z]+", value)
    if len(words) < max(head, tail) + 2:
        return []
    return ["starts: " + " ".join(words[:head]), "ends: " + " ".join(words[-tail:])]


def template_report(added_lines, minimum=10):
    """How much of a change is one sentence pattern repeated.

    Returns None below ``minimum`` prose lines, else the most repeated opening or
    ending, its count, and its share of prose lines. Tables and code rows are
    ignored: repetition there is normal.
    """
    prose = []
    for line in added_lines:
        stripped = line.strip()
        # Judge the prose inside a JSON value or string literal, not the code around it.
        literals = re.findall(r'"((?:[^"\\]|\\.){30,})"|\'((?:[^\'\\]|\\.){30,})\'', stripped)
        value = max((a or b for a, b in literals), key=len) if literals else stripped
        if value.startswith(("|", "#", "-", "{", "}", "[", "]")) or value.count(";") >= 2:
            continue
        # Paths, ids and urls repeat by nature in reports and manifests; repetition
        # there says nothing about whether the prose was written per item.
        if re.match(r"^[\w./-]+$", value) or value.count("/") >= 2 or "://" in value:
            continue
        if skeleton_keys(value):
            prose.append(value)
    if len(prose) < minimum:
        return None
    counts = {}
    for value in prose:
        for key in skeleton_keys(value):
            counts[key] = counts.get(key, 0) + 1
    key, count = max(counts.items(), key=lambda item: item[1])
    return {"prose_lines": len(prose), "pattern": key, "count": count,
            "share": round(count / len(prose), 3)}


def decide(obs):
    """The next supervisor action from one turn's observation. No model call.

    ``obs`` keys: supervisor_unavailable, available (best first, current provider
    excluded), earliest_reset_seconds, edited_files, large_code, violations,
    context_tokens, reset_at, stagnant_turns, stagnant_limit.
    """
    available = list(obs.get("available") or [])
    if obs.get("supervisor_unavailable"):
        if available:
            return _decision("switch_supervisor", "1: supervisor unavailable", provider=available[0])
        wait = obs.get("earliest_reset_seconds") or 1800
        return _decision("wait", "1: no supervisor available", wait_seconds=max(60, int(wait)))
    if obs.get("edited_files") or obs.get("large_code"):
        if obs.get("violations", 0) >= 2:
            return _decision("reset_context", "2: repeated supervisor violation", reminder=REMINDER)
        return _decision("remind", "2: supervisor did work itself", reminder=REMINDER)
    if obs.get("reset_at") and obs.get("context_tokens", 0) >= obs["reset_at"]:
        return _decision("reset_context", "3: context over threshold")
    if obs.get("stagnant_turns", 0) >= obs.get("stagnant_limit", 4):
        if available:
            return _decision("switch_supervisor", "4: stagnant, rotating supervisor",
                             provider=available[0])
        return _decision("reset_context", "4: stagnant, fresh session")
    return _decision("continue", "5: healthy")


def _decision(action, rule, provider=None, wait_seconds=0, reminder=None):
    return {"action": action, "rule": rule, "provider": provider,
            "wait_seconds": wait_seconds, "reminder": reminder}


class Ollama:
    """Minimal stdlib client for a local Ollama server."""

    def __init__(self, model=DEFAULT_KEEPER_MODEL, url=OLLAMA_URL, timeout=120):
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout
        self.spawned_at = 0.0

    def _post(self, path, body, timeout):
        request = urllib.request.Request(self.url + path, json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def alive(self):
        try:
            with urllib.request.urlopen(self.url + "/api/version", timeout=3) as response:
                return response.status == 200
        except (OSError, ValueError):
            return False

    def ensure(self):
        """Start the server if it is down and confirm the model exists."""
        if not self.alive():
            if not shutil.which("ollama") or time.time() - self.spawned_at < 600:
                return False
            self.spawned_at = time.time()
            subprocess.Popen(["ollama", "serve"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            for _ in range(30):
                time.sleep(1)
                if self.alive():
                    break
            else:
                return False
        try:
            self._post("/api/show", {"model": self.model}, 10)
            return True
        except (OSError, ValueError, urllib.error.HTTPError):
            return False

    def chat(self, system, user, schema):
        data = self._post("/api/chat", {
            # Summaries arrive minutes apart; holding ~5 GB of model in memory
            # between them starved the machine. Reloading costs seconds.
            "model": self.model, "stream": False, "format": schema, "keep_alive": "2m",
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}, self.timeout)
        return json.loads(data["message"]["content"])


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "blocker": {"type": ["string", "null"]},
        "suggested_fix_command": {"type": ["string", "null"]},
        "progress": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "handoff": {"type": "string"},
    },
    "required": ["blocker", "suggested_fix_command", "progress", "handoff"],
}
SUMMARY_SYSTEM = ("You summarize a coding worker's log for the next supervisor session. "
                  "Be factual and quote only what is in the log. 'blocker' is what still "
                  "prevents the task from passing, or null. 'handoff' is at most 3 sentences.")
HINT_PATTERN = re.compile(
    r"(?i)(error|exception|traceback|failed|not found|doesn't exist|does not exist|missing|"
    r"cannot|npx |pip install|npm install|playwright install)")

SPECIFIC_PATTERN = re.compile(
    r"(?i)(not found|doesn't exist|does not exist|cannot find|no module|enoent|command not found)")
COMMAND_PATTERN = re.compile(r"(npx [\w@./-]+(?: [\w@./-]+)*|pip install [\w@.=<>-]+|npm install[\w@./ -]*)")


def fallback_summary(log_tail):
    lines = [line.strip() for line in str(log_tail or "").splitlines() if line.strip()]
    hints = [line[:240] for line in lines if HINT_PATTERN.search(line)][-6:]
    specific = [line for line in hints if SPECIFIC_PATTERN.search(line)]
    commands = [match.group(0).strip("`║ ") for line in hints
                for match in [COMMAND_PATTERN.search(line)] if match]
    return {"blocker": (specific or hints or [None])[-1],
            "suggested_fix_command": commands[-1] if commands else None,
            "progress": [], "handoff": " | ".join(hints) or " | ".join(lines[-3:])[:600]}


CLEAN_OUTCOMES = ("passed", "accepted")
SUCCESS_CLAIM = re.compile(
    r"(?i)\ball (the )?tests (now )?pass(ed)?\b|\btask (is |was |has been )?(now )?"
    r"(complete|completed|done|finished)\b|\bcompleted successfully\b|\bnothing is blocking\b"
    r"|\bno (remaining )?blockers?\b")


def logged_commands(log_tail):
    return {match.group(0).strip("`║ ") for line in str(log_tail or "").splitlines()
            for match in [COMMAND_PATTERN.search(line)] if match}


def drop_success_claims(text):
    """``text`` without the sentences that claim success, which a failed outcome disproves."""
    sentences = re.split(r"(?<=[.!?])\s+", str(text or ""))
    return " ".join(sentence for sentence in sentences if not SUCCESS_CLAIM.search(sentence)).strip()


def guard_summary(value, outcome, log_tail):
    """Constrain a model summary by what the caller knows for certain.

    Log text is untrusted: a worker, or a file it printed, can tell the model to
    blank the blocker or to suggest a command. The outcome comes from exit codes,
    so a failed run never keeps an empty blocker or a sentence claiming success,
    and only an install-style command printed in the log survives as a hint.
    """
    # Models wrap the command in prose ("Run `npx playwright install` to ..."); keep
    # the longest logged command it mentions rather than demanding the bare command.
    suggested = str(value.get("suggested_fix_command") or "")
    mentioned = [command for command in logged_commands(log_tail) if command in suggested]
    value["suggested_fix_command"] = max(mentioned, key=len) if mentioned else None
    if outcome not in CLEAN_OUTCOMES:
        if not str(value.get("blocker") or "").strip():
            value["blocker"] = (fallback_summary(log_tail)["blocker"]
                                or outcome + ": no error line recognised in the log; read the log tail")
        handoff = drop_success_claims(value.get("handoff"))
        if handoff != str(value.get("handoff") or "").strip():
            value["handoff"] = (outcome + " by exit code; the worker's claim of success was removed. "
                                + (handoff or fallback_summary(log_tail)["handoff"]))[:800]
        value["progress"] = [item for item in value.get("progress") or [] if not SUCCESS_CLAIM.search(item)]
    return value


INJECTION_PATTERN = re.compile(
    r"(?i)ignore (all )?(previous|prior|above) instructions|note to (the )?(summari[sz]er|assistant|ai)\b"
    r"|\b(ai|llm) (assistant|agent)s? (reading|who)|</?system>"
    r"|\bset (the )?(blocker|suggested_fix_command) (to|as)\b|\bleave (the )?blocker (empty|null|blank)\b")
INJECTION_NOTE = "[log contained instructions aimed at the summariser; summarised without the model] "


def sanitised_fallback(outcome, log_tail):
    """Deterministic summary of a log whose instruction-like lines are removed."""
    clean = "\n".join(line for line in str(log_tail or "").splitlines()
                      if not INJECTION_PATTERN.search(line))
    value = guard_summary(fallback_summary(clean), outcome, clean)
    if INJECTION_PATTERN.search(str(log_tail or "")):
        value["handoff"] = INJECTION_NOTE + value["handoff"]
    return value


CLASSIFY_SCHEMA = {"type": "object", "properties": {"class": {"type": "string", "enum": [
    diagnosis.MISSING_DEPENDENCY, diagnosis.TIMEOUT, diagnosis.ASSERTION, diagnosis.SYNTAX,
    diagnosis.PERMISSION, diagnosis.UNKNOWN]}}, "required": ["class"]}
CLASSIFY_SYSTEM = (
    "Classify one failure diagnostic from a coding task into exactly one class:\n"
    "missing_dependency: a package, module, crate or command is not installed.\n"
    "timeout: an operation ran out of time or a connection timed out.\n"
    "assertion: a test ran and an expected value did not match.\n"
    "syntax: the code could not be parsed or compiled because of its syntax.\n"
    "permission: the OS refused access.\n"
    "unknown: anything else, including missing data files, crashes and killed processes.\n"
    "Judge the actual error, not words that appear in test or file names.")

# Worker claims are checked by rule, not by the model. On a realistic multi-paragraph
# reply qwen2.5-coder:7b flagged follow-up notes and edits inside changed files, and
# missed real false claims, differently with each prompt wording.
CHANGE_VERB = re.compile(r"(?i)\b(add(ed)?|creat(ed|e)|wr(o|i)te|written|updat(ed|e)|chang(ed|e)|"
                         r"modif(ied|y)|document(ed)?|implement(ed)?|fix(ed)?|mov(ed|e)|renam(ed|e)|"
                         r"delet(ed|e)|remov(ed|e)|migrat\w*|cover(ed)?|extend(ed)?|refactor(ed)?)\b")
# Adding or documenting an artifact needs a file of that kind; fixing a test does not.
ARTIFACT_VERB = re.compile(r"(?i)\b(add(ed)?|creat(ed|e)|wr(o|i)te|written|document(ed)?|"
                           r"cover(ed)?|updat(ed|e)|extend(ed)?)\b")
NOT_A_CLAIM = re.compile(r"(?i)\b(not|never|no longer|may|might|could|should|would|worth|"
                         r"follow-?up|todo|later)\b|n't\b")
PATH_TOKEN = re.compile(r"(?<![\w/.-])[\w./-]*\.(?:py|ts|tsx|js|jsx|mjs|md|rst|json|ya?ml|toml|css|"
                        r"scss|html|go|rs|rb|java|kt|swift|sql|sh)\b")
ARTIFACTS = (
    ("tests", re.compile(r"(?i)\b(unit |regression |integration )?(tests?|specs?)\b"),
     re.compile(r"(?i)(^|/)(tests?|specs?|__tests__)/|[._-](test|spec)s?\.|(^|/)test_")),
    ("docs", re.compile(r"(?i)\b(docs?|documentation|readme|changelog)\b|\bdocumented\b"),
     re.compile(r"(?i)\.(md|rst|txt|adoc)$|(^|/)docs?/")),
    ("migration", re.compile(r"(?i)\bmigrations?\b"), re.compile(r"(?i)migrat")),
)


def unsupported_claims(said, files):
    """Sentences where the worker says it changed something no changed file shows.

    A sentence counts when it uses a change verb and is not negated or hedged
    ("did not", "may be worth", "follow-up"). It is unsupported when it names a
    file that did not change, or says it added tests, docs or a migration and no
    changed file is of that kind. Advisory: the supervisor decides what it means.
    """
    changed = [name.strip("./") for name in files]
    flagged = []
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(str(said or "").split())):
        if not CHANGE_VERB.search(sentence) or NOT_A_CLAIM.search(sentence):
            continue
        paths = [path.strip("./") for path in PATH_TOKEN.findall(sentence)]
        missing = [path for path in paths
                   if not any(name == path or name.endswith("/" + path) for name in changed)]
        kinds = [kind for kind, mention, pattern in ARTIFACTS
                 if ARTIFACT_VERB.search(sentence) and mention.search(sentence)
                 and not any(pattern.search(name) for name in changed)]
        if missing or kinds:
            flagged.append(sentence[:240])
    return flagged[:5]

class Scribe:
    """Compresses worker results with the local model, or deterministically.

    It also names a failure class where the regexes in diagnosis.py find none,
    as an advisory note for the supervisor; it feeds no keeper decision.
    """

    def __init__(self, client=None):
        self.client = client

    def ask(self, system, user, schema):
        """The model's JSON reply, or None when there is no model or it fails."""
        if self.client is None:
            return None
        try:
            if self.client.ensure():
                value = self.client.chat(system, user, schema)
                return value if isinstance(value, dict) else None
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
            pass
        return None

    def summarise(self, task_id, outcome, log_tail):
        """Returns (summary, source). ``outcome`` is decided by the caller, never here."""
        tail = str(log_tail or "")[-6000:]
        if INJECTION_PATTERN.search(tail):
            return sanitised_fallback(outcome, tail), "fallback"
        value = self.ask(SUMMARY_SYSTEM, "TASK: " + task_id + "\nOUTCOME (from exit codes): "
                         + outcome + "\nLOG:\n" + tail, SUMMARY_SCHEMA)
        if value and isinstance(value.get("handoff"), str) and isinstance(value.get("progress"), list):
            value["progress"] = [str(item)[:240] for item in value["progress"][:4]]
            value["handoff"] = value["handoff"][:800]
            return guard_summary(value, outcome, tail), "local-model"
        return sanitised_fallback(outcome, tail), "fallback"

    def classify(self, text):
        """(class, source): diagnosis.classify, asking the model only when it says unknown.

        Measured on 14 diagnostics the regexes alone got 50% right; adding
        qwen2.5-coder:7b for their unknowns got 86%. The regex answer is kept
        whenever it has one, so engine fingerprints and this note agree.
        """
        found = diagnosis.classify(text)
        tail = str(text or "")[-3000:]
        if found != diagnosis.UNKNOWN or not tail.strip() or INJECTION_PATTERN.search(tail):
            return found, "regex"
        name = (self.ask(CLASSIFY_SYSTEM, "DIAGNOSTIC:\n" + tail, CLASSIFY_SCHEMA) or {}).get("class")
        if name in CLASSIFY_SCHEMA["properties"]["class"]["enum"]:
            return name, "local-model"
        return found, "regex"
