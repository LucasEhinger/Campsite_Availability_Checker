"""Shared SMTP sending for the campsite watchers.

Defaults to Gmail; any provider works by setting SMTP_HOST / SMTP_PORT.
Required secrets: SMTP_USER, SMTP_PASS (a 16-character Gmail App Password),
and TO_EMAIL for the recipient.
"""

import os


def describe_email_env():
    """Report the shape of the mail secrets without revealing them.

    Log output is readable by anyone who can see the repo, so this prints
    only lengths and shape checks -- never the values themselves.
    """
    print("Mail settings (SMTP):")
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "TO_EMAIL"):
        raw = os.environ.get(name)
        if not raw:
            default = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587"}.get(name)
            print(f"  {name}: not set" + (f" (defaulting to {default})" if default else ""))
            continue
        notes = []
        if raw != raw.strip():
            notes.append("HAS SURROUNDING WHITESPACE (likely the problem)")
        if name == "SMTP_PASS":
            stripped = raw.replace(" ", "").strip()
            notes.append(f"{len(stripped)} chars once spaces are removed")
            if len(stripped) != 16:
                notes.append("NOTE: a Gmail app password is 16 characters")
        elif name in ("SMTP_USER", "TO_EMAIL"):
            notes.append("looks like an address" if "@" in raw and "." in raw.split("@")[-1]
                         else "does NOT look like an email address")
        else:
            notes.append(raw)
        print(f"  {name}: {len(raw)} chars, " + ", ".join(notes))


def send_email(subject, html):
    """Send through SMTP. Defaults to Gmail; any provider works via SMTP_HOST."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    missing = [k for k in ("SMTP_USER", "SMTP_PASS") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing environment variable(s): {', '.join(missing)}. "
            "Set them with: gh secret set SMTP_USER / gh secret set SMTP_PASS"
        )

    # An unset GitHub secret arrives as "", so `or` rather than a get() default.
    host = (os.environ.get("SMTP_HOST") or "smtp.gmail.com").strip()
    port = int((os.environ.get("SMTP_PORT") or "587").strip())
    user = os.environ["SMTP_USER"].strip()
    # Google displays app passwords in groups of four; the spaces are cosmetic.
    password = os.environ["SMTP_PASS"].replace(" ", "").strip()
    recipient = (os.environ.get("TO_EMAIL") or user).strip()
    # Gmail rewrites (or rejects) a From that isn't the authenticated account.
    sender = user if "gmail" in host else (os.environ.get("FROM_EMAIL") or user).strip()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content("Peddocks Island availability alert -- view in HTML.")
    message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            f"SMTP login rejected by {host}: {exc}\n"
            "  -> For Gmail this must be a 16-character App Password, not your\n"
            "     normal password, and 2-Step Verification must be on.\n"
            "     Create one at https://myaccount.google.com/apppasswords\n"
            "     then run: gh secret set SMTP_PASS"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"SMTP send via {host}:{port} failed: {exc}") from exc
    # Not logging the recipient: run logs are public, and GitHub's secret
    # masking is best-effort once a value has been transformed.
    print(f"Email sent via {host}.")
