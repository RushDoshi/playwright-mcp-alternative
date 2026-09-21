"""Jira Cloud client (Layer A).

Source of the requirement AND the approval gate. Reads the ticket (description + comments
bottom-to-top), posts the plan as a comment, detects approval, and drives lifecycle
transitions (In Progress, In Review).

Network calls go through httpx. They are isolated in small methods so tests can mock them
with respx. Auth is Basic (email + API token) from host settings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import Settings


# Prefixes this system's own workflows post back to a ticket. Excluded from
# requirement_text() so re-reading a ticket the automation already ran on
# doesn't keep seeing (and re-hashing design_scenarios()'s cache key on) its
# own prior output -- every run would otherwise add new content and
# permanently invalidate the design cache for that ticket. Comment counting
# (comment_count(), the Gate-1 approval baseline) is untouched -- it must stay
# a raw, unfiltered count.
_BOT_COMMENT_PREFIXES = (
    # "# Plan for "/"# Revised plan for " (with the literal "#") match a plan
    # comment posted before _text_to_adf rendered "# " as a real ADF heading;
    # once rendered as a heading, reading it back via _adf_to_text flattens
    # to the text alone, no "#" — both forms must match so old and new plan
    # comments are excluded from requirement_text() alike.
    "# Plan for ", "Plan for ", "# Revised plan for ", "Revised plan for ",
    "Implementation complete for ",
    "UI automation FAILED: ", "PR opened: ", "Automation FAILED: ",
)


@dataclass
class JiraTicket:
    key: str
    summary: str
    description: str
    comments_newest_first: list[str] = field(default_factory=list)
    status: str = ""

    def requirement_text(self) -> str:
        """Description + comments, newest comment first (bottom-to-top reading).
        Comments this same automation posted back to the ticket are excluded —
        see _BOT_COMMENT_PREFIXES."""
        human_comments = [c for c in self.comments_newest_first
                          if not c.startswith(_BOT_COMMENT_PREFIXES)]
        parts = [f"# {self.key}: {self.summary}", "", self.description, "", "## Comments (newest first)"]
        parts.extend(f"- {c}" for c in human_comments)
        return "\n".join(parts)


@dataclass
class JiraAttachment:
    filename: str
    url: str
    size: int = 0


_APPROVAL_RE = re.compile(r"\b(approved?|lgtm|go ahead|ship it|proceed|looks good)\b", re.I)
_REPO_RE = re.compile(r"repo[:=]\s*([A-Za-z0-9._-]+)")


class JiraClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.s = settings
        self._client = client or httpx.Client(
            base_url=settings.jira_base_url,
            auth=(settings.jira_email, settings.jira_api_token),
            # Attachment content requests 303-redirect to Atlassian's media
            # service (a different host, carrying its own signed token) —
            # httpx does not follow redirects by default.
            follow_redirects=True,
            timeout=30,
        )

    # --- reads ---
    def fetch_ticket(self, key: str) -> JiraTicket:
        issue = self._client.get(f"/rest/api/3/issue/{key}", params={"expand": "renderedFields"}).json()
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        description = _adf_to_text(fields.get("description"))
        status = fields.get("status", {}).get("name", "")
        comments = self._fetch_comments(key)
        return JiraTicket(key=key, summary=summary, description=description,
                          comments_newest_first=comments, status=status)

    def _fetch_comments(self, key: str) -> list[str]:
        """All comments on a ticket, oldest-fetched first internally (reversed
        at the end for the newest-first/bottom-to-top contract).

        Paginates: Jira Cloud caps a single page at 100 regardless of the
        maxResults requested. A ticket that crosses 100 comments (this
        happened for real, mid-demo-prep) would otherwise silently lose
        everything past #100 — including a genuine new Gate-1 approval reply,
        invisible to comment_count()'s baseline and the approval poller alike.
        """
        comments: list[str] = []
        start_at = 0
        while True:
            data = self._client.get(f"/rest/api/3/issue/{key}/comment",
                                    params={"startAt": start_at, "maxResults": 100}).json()
            page = data.get("comments", [])
            comments.extend(_adf_to_text(c.get("body")) for c in page)
            start_at += len(page)
            if not page or start_at >= data.get("total", start_at):
                break
        return list(reversed(comments))  # newest first == bottom-to-top

    def extract_repo(self, ticket: JiraTicket) -> str | None:
        m = _REPO_RE.search(ticket.requirement_text())
        return m.group(1) if m else None

    def search_issues(self, jql: str, max_results: int = 50) -> list[str]:
        """Issue keys matching a JQL query — used by the Ready-for-Development
        poller to discover candidate tickets without a Jira-side webhook."""
        data = self._client.get("/rest/api/3/search",
                                params={"jql": jql, "maxResults": max_results,
                                        "fields": "key"}).json()
        return [issue["key"] for issue in data.get("issues", [])]

    def comment_count(self, key: str) -> int:
        """Total comments on the ticket right now — used as the Gate-1 baseline so the
        approval poller only looks at comments posted *after* the plan was posted."""
        return len(self._fetch_comments(key))

    def fetch_attachments(self, key: str) -> list[JiraAttachment]:
        """List attachments on a ticket (e.g. test files for an upload scenario)."""
        issue = self._client.get(f"/rest/api/3/issue/{key}", params={"fields": "attachment"}).json()
        atts = issue.get("fields", {}).get("attachment", [])
        return [JiraAttachment(filename=a.get("filename", ""), url=a.get("content", ""),
                               size=int(a.get("size") or 0)) for a in atts]

    def download_attachment(self, attachment: JiraAttachment, dest_dir: Path) -> Path:
        """Download one attachment's binary content into ``dest_dir``. Same Basic-auth
        client as everything else — Jira attachment URLs are absolute, so httpx uses
        them as-is regardless of the client's base_url."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / attachment.filename
        with self._client.stream("GET", attachment.url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        return dest

    # --- writes ---
    def post_plan_comment(self, key: str, plan_markdown: str) -> str:
        body = _text_to_adf(plan_markdown)
        resp = self._client.post(f"/rest/api/3/issue/{key}/comment", json={"body": body}).json()
        return resp.get("self", "")  # comment URL (Gate 1 location)

    def post_comment(self, key: str, text: str) -> None:
        self._client.post(f"/rest/api/3/issue/{key}/comment", json={"body": _text_to_adf(text)})

    def is_approval(self, comment_text: str) -> bool:
        return bool(_APPROVAL_RE.search(comment_text))

    def transition(self, key: str, target_status: str) -> None:
        """Move the ticket to a named status (e.g. In Progress, In Review)."""
        transitions = self._client.get(f"/rest/api/3/issue/{key}/transitions").json().get("transitions", [])
        match = next((t for t in transitions if t["to"]["name"].lower() == target_status.lower()), None)
        if match:
            self._client.post(f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": match["id"]}})


# --- ADF helpers (Atlassian Document Format) ---
def _adf_to_text(adf) -> str:
    """Flatten ADF (or plain string) into text."""
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node.get("text", ""))
            elif node.get("type") == "hardBreak":
                out.append("\n")
            for child in node.get("content", []):
                walk(child)
            if node.get("type") in ("paragraph", "heading"):
                out.append("\n")
        elif isinstance(node, list):
            for n in node:
                walk(n)

    walk(adf)
    return "".join(out).strip()


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^-\s+(.*)$")
# **bold**, `code`, _italic_ — the only inline markup this codebase's generated
# plans/comments actually use (see uitest_workflow.py's _plan_markdown).
# Underscore emphasis excludes intraword underscores (CommonMark's own rule for
# '_', unlike '*') via the lookaround guards below — without it, a literal
# identifier like "view_file_preview" in plan/comment text gets its underscores
# eaten as emphasis markers, squashing it into "viewfilepreview" (confirmed
# live: this exact action name rendered that way in a posted Jira plan).
_INLINE_RE = re.compile(
    r"\*\*(?P<bold>[^*]+)\*\*|`(?P<code>[^`]+)`|(?<!\w)_(?P<italic>[^_]+)_(?!\w)")


def _inline_content(line: str) -> list[dict]:
    """Split one line into ADF text nodes, applying strong/code/em marks to
    **bold**/`code`/_italic_ spans so they render as real formatting instead of
    literal asterisks/backticks/underscores in the Jira comment."""
    content: list[dict] = []
    pos = 0
    for m in _INLINE_RE.finditer(line):
        if m.start() > pos:
            content.append({"type": "text", "text": line[pos:m.start()]})
        if m.group("bold") is not None:
            content.append({"type": "text", "text": m.group("bold"),
                            "marks": [{"type": "strong"}]})
        elif m.group("code") is not None:
            content.append({"type": "text", "text": m.group("code"),
                            "marks": [{"type": "code"}]})
        else:
            content.append({"type": "text", "text": m.group("italic"),
                            "marks": [{"type": "em"}]})
        pos = m.end()
    if pos < len(line):
        content.append({"type": "text", "text": line[pos:]})
    return content


def _paragraph_content(paragraph: str) -> list[dict]:
    """A single ADF text node never renders a bare '\\n' as a line break —
    Jira needs an explicit hardBreak node between lines, or a multi-line
    comment collapses onto one run-on line."""
    content: list[dict] = []
    for i, line in enumerate(paragraph.split("\n")):
        if i > 0:
            content.append({"type": "hardBreak"})
        if line:
            content.extend(_inline_content(line))
    return content


def _text_to_adf(text: str) -> dict:
    """Render markdown into real ADF blocks instead of dumping every line as flat
    plain-paragraph text: '#'/'##'/... lines become heading nodes, consecutive
    '- ' lines become one bulletList, blank-line-separated runs of ordinary text
    become paragraphs (hardBreak between their lines — see _paragraph_content),
    and **bold**/`code`/_italic_ spans get real marks (see _inline_content).
    Without this, a plan posted to Jira shows literal '#'/'-'/'**' characters in
    one flat run of paragraphs no matter how it's structured on the Python side.
    """
    lines = text.split("\n")
    content: list[dict] = []
    para_lines: list[str] = []

    def flush_paragraph() -> None:
        if para_lines:
            content.append({"type": "paragraph",
                            "content": _paragraph_content("\n".join(para_lines))})
            para_lines.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        heading_m = _HEADING_RE.match(line)
        bullet_m = _BULLET_RE.match(line)
        if heading_m:
            flush_paragraph()
            level = min(len(heading_m.group(1)), 6)
            content.append({"type": "heading", "attrs": {"level": level},
                            "content": _inline_content(heading_m.group(2)) or
                            [{"type": "text", "text": ""}]})
            i += 1
        elif bullet_m:
            flush_paragraph()
            items = []
            while i < len(lines) and (m := _BULLET_RE.match(lines[i])):
                items.append({"type": "listItem", "content": [
                    {"type": "paragraph", "content": _inline_content(m.group(1))}]})
                i += 1
            content.append({"type": "bulletList", "content": items})
        elif line.strip() == "":
            flush_paragraph()
            i += 1
        else:
            para_lines.append(line)
            i += 1
    flush_paragraph()
    return {"type": "doc", "version": 1, "content": content or
            [{"type": "paragraph", "content": _paragraph_content(text)}]}
