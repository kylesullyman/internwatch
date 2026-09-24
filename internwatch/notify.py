"""Notification channels. Each is enabled by its env vars being set, so secrets never live in config.

  ntfy     NTFY_TOPIC (+ optional NTFY_SERVER, NTFY_TOKEN)   push to phone, PDF attached
  email    SMTP_HOST SMTP_USER SMTP_PASS EMAIL_TO (+ SMTP_PORT)  full change summary + PDF
  discord  DISCORD_WEBHOOK_URL                                message + PDF
"""
from __future__ import annotations

import base64
import mimetypes
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from .http import session
from .models import Job


@dataclass
class Alert:
    job: Job
    headline: str                     # one line: why this matters
    body_md: str = ""                 # full markdown summary (tailoring changes etc.)
    push_lines: list[str] = field(default_factory=list)   # short lines for the phone
    attachments: list[Path] = field(default_factory=list)
    priority: int = 3                 # ntfy 1-5


def _rfc2047(s: str) -> str:
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(s.encode()).decode() + "?="


def _one_line(s: str) -> str:
    return " ".join(s.split())


class Notifier:
    def __init__(self, dry_run: bool = False, email: bool = True):
        self.dry = dry_run
        e = os.environ.get
        self.ntfy_topic = e("NTFY_TOPIC")
        self.ntfy_server = (e("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
        self.ntfy_token = e("NTFY_TOKEN")
        self.smtp = (e("SMTP_HOST"), int(e("SMTP_PORT") or 465), e("SMTP_USER"), e("SMTP_PASS"), e("EMAIL_TO"))
        self.email_enabled = email
        self.discord = e("DISCORD_WEBHOOK_URL")

    @property
    def channels(self) -> list[str]:
        c = []
        if self.ntfy_topic:
            c.append("ntfy")
        if self.email_enabled and all(self.smtp[i] for i in (0, 2, 3, 4)):
            c.append("email")
        if self.discord:
            c.append("discord")
        return c

    def _guard(self, ch: str, fn, *a, **kw) -> None:
        """Run one channel's send. A broken channel must never take down the run:
        the poll already succeeded and the logbook is already on disk by this point."""
        try:
            fn(*a, **kw)
        except Exception as ex:  # noqa: BLE001
            print(f"  ! {ch} failed: {ex}")

    def send(self, a: Alert) -> None:
        if self.dry:
            print(f"\n[DRY RUN] {a.headline}\n  {a.job.url}\n  " + "\n  ".join(a.push_lines)
                  + (f"\n  attachments: {[str(p) for p in a.attachments]}" if a.attachments else ""))
            return
        for ch in self.channels:
            self._guard(ch, getattr(self, f"_{ch}"), a)

    def digest(self, jobs: list[Job]) -> None:
        """Lower-priority matches, batched into one push per run."""
        if not jobs:
            return
        lines = [f"• {j.short()}\n  {j.url}" for j in jobs[:25]]
        more = f"\n…and {len(jobs) - 25} more" if len(jobs) > 25 else ""
        if self.dry:
            print(f"\n[DRY RUN digest] {len(jobs)} other matches\n" + "\n".join(lines) + more)
            return
        if self.ntfy_topic:
            self._guard("ntfy", self._ntfy_raw, title=f"{len(jobs)} more internship matches",
                        message="\n".join(lines) + more, priority=2, tags="clipboard")
        if "email" in self.channels:
            self._guard("email", self._email_raw, f"[internwatch] {len(jobs)} more matches",
                        "\n".join(lines) + more, [])

    # ---- channels -----------------------------------------------------------------
    def _ntfy_raw(self, title: str, message: str, priority: int = 3, tags: str = "", click: str = "",
                  actions: str = "", attach: Path | None = None) -> None:
        h = {"Title": _rfc2047(_one_line(title)[:250]), "Priority": str(priority)}
        if tags:
            h["Tags"] = tags
        if click:
            h["Click"] = click
        if actions:
            h["Actions"] = _rfc2047(actions)
        if self.ntfy_token:
            h["Authorization"] = f"Bearer {self.ntfy_token}"
        url = f"{self.ntfy_server}/{self.ntfy_topic}"
        if attach:
            # With a file body, the text has to travel in a header.
            h["Filename"] = attach.name
            h["Message"] = _rfc2047(message.replace("\n", "\\n")[:3500])
            r = session().put(url, data=attach.read_bytes(), headers=h, timeout=60)
        else:
            r = session().post(url, data=message.encode(), headers=h, timeout=30)
        r.raise_for_status()

    def _ntfy(self, a: Alert) -> None:
        pdf = next((p for p in a.attachments if p.suffix == ".pdf"), None)
        self._ntfy_raw(
            title=a.headline,
            message="\n".join([a.job.short()] + a.push_lines),
            priority=a.priority,
            tags="briefcase",
            click=a.job.url,
            actions=f"view, Apply, {a.job.url}",
            attach=pdf,
        )

    def _email_raw(self, subject: str, body: str, attachments: list[Path], html: str | None = None) -> None:
        host, port, user, pw, to = self.smtp
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        for p in attachments:
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)

    def _email(self, a: Alert) -> None:
        body = f"{a.headline}\n\n{a.job.short()}\nApply: {a.job.url}\n\n{a.body_md}"
        self._email_raw(f"[internwatch] {a.job.company}: {a.job.title}", body, a.attachments, _md_to_html(body))

    def _discord(self, a: Alert) -> None:
        content = f"**{a.headline}**\n{a.job.short()}\n<{a.job.url}>\n" + "\n".join(a.push_lines)
        files = {f"files[{i}]": (p.name, p.read_bytes()) for i, p in enumerate(a.attachments[:4])}
        r = session().post(self.discord, data={"content": content[:1990]}, files=files or None, timeout=60)
        r.raise_for_status()


def _md_to_html(md: str) -> str:
    """Tiny markdown→HTML so the email is readable without adding a dependency."""
    import html as h
    import re
    out, in_list = [], False
    for line in md.splitlines():
        esc = h.escape(line)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"~~(.+?)~~", r"<s style='color:#a33'>\1</s>", esc)
        esc = re.sub(r"`(.+?)`", r"<code>\1</code>", esc)
        esc = re.sub(r"(https?://\S+)", r"<a href='\1'>\1</a>", esc)
        if line.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{esc[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"(#+) (.*)", line)
        if m:
            n = min(len(m.group(1)) + 1, 4)
            out.append(f"<h{n}>{h.escape(m.group(2))}</h{n}>")
        elif line.strip():
            out.append(f"<p style='margin:4px 0'>{esc}</p>")
    if in_list:
        out.append("</ul>")
    return "<div style='font-family:system-ui,sans-serif;max-width:720px'>" + "\n".join(out) + "</div>"
