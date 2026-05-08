---
name: transactional-email
description: Adds transactional email sending to a Lauren application using an abstract EmailBackend. Use when sending welcome emails, password reset links, order confirmations, or any system-triggered email. Supports SMTP, SendGrid, AWS SES, and an in-memory backend for tests.
---

> Use `codemap find "EmailService"` to check if an email service is already wired before adding another.

# Transactional Email Service

The pattern uses an abstract `EmailBackend` so the in-memory implementation can be used in tests and swapped for SMTP, SendGrid, or SES in production.

## Core types

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

@dataclass
class EmailMessage:
    to: list[str]
    subject: str
    body: str
    html_body: str = ""
    from_addr: str = "noreply@example.com"
    reply_to: str = ""

class EmailBackend(ABC):
    @abstractmethod
    async def send(self, message: EmailMessage) -> None: ...
```

## In-memory backend (tests / development)

```python
class InMemoryEmailBackend(EmailBackend):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def find(self, to: str) -> list[EmailMessage]:
        return [m for m in self.sent if to in m.to]

    def clear(self) -> None:
        self.sent.clear()
```

## SMTP backend

```python
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

class SMTPEmailBackend(EmailBackend):
    def __init__(self, host: str, port: int = 587, username: str = "", password: str = "") -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password

    async def send(self, message: EmailMessage) -> None:
        import anyio
        await anyio.to_thread.run_sync(self._send_sync, message)

    def _send_sync(self, message: EmailMessage) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = message.subject
        msg["From"] = message.from_addr
        msg["To"] = ", ".join(message.to)
        msg.attach(MIMEText(message.body, "plain"))
        if message.html_body:
            msg.attach(MIMEText(message.html_body, "html"))
        context = ssl.create_default_context()
        with smtplib.SMTP(self._host, self._port) as server:
            server.starttls(context=context)
            if self._username:
                server.login(self._username, self._password)
            server.sendmail(message.from_addr, message.to, msg.as_string())
```

## SendGrid backend

```python
class SendGridEmailBackend(EmailBackend):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def send(self, message: EmailMessage) -> None:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "personalizations": [{"to": [{"email": addr} for addr in message.to]}],
                    "from": {"email": message.from_addr},
                    "subject": message.subject,
                    "content": [
                        {"type": "text/plain", "value": message.body},
                        *([{"type": "text/html", "value": message.html_body}] if message.html_body else []),
                    ],
                },
            )
```

## Email service

```python
from lauren import injectable, Scope
from lauren.di import use_value

@injectable(scope=Scope.SINGLETON)
class EmailService:
    def __init__(self, backend: EmailBackend) -> None:
        self._backend = backend

    async def send_welcome_email(self, to_addr: str, username: str) -> None:
        await self._backend.send(EmailMessage(
            to=[to_addr],
            subject=f"Welcome, {username}!",
            body=f"Hi {username}, welcome to our platform.",
            html_body=f"<h1>Welcome {username}</h1><p>We're glad you're here.</p>",
        ))

    async def send_password_reset(self, to_addr: str, reset_link: str) -> None:
        await self._backend.send(EmailMessage(
            to=[to_addr],
            subject="Reset your password",
            body=f"Click here to reset your password: {reset_link}",
            html_body=f'<p>Click <a href="{reset_link}">here</a> to reset your password.</p>',
        ))

    async def send_order_confirmation(self, to_addr: str, order_id: str, total: float) -> None:
        await self._backend.send(EmailMessage(
            to=[to_addr],
            subject=f"Order #{order_id} confirmed",
            body=f"Your order #{order_id} for ${total:.2f} has been confirmed.",
        ))
```

## Module wiring

```python
from lauren import module, use_value

backend = InMemoryEmailBackend()  # swap for SMTP/SendGrid in production

@module(providers=[
    use_value(provide=EmailBackend, value=backend),
    EmailService,
])
class EmailModule:
    pass
```

For production, replace the `use_value` with the real backend:

```python
from lauren import use_class

@module(providers=[
    use_class(provide=EmailBackend, use_class=SendGridEmailBackend),
    EmailService,
])
class EmailModule:
    pass
```

## Key points

- `EmailBackend` is the injection token; swap backends without changing `EmailService`.
- Wrap sync SMTP calls in `anyio.to_thread.run_sync` to avoid blocking the event loop.
- `InMemoryEmailBackend.find(to)` makes test assertions readable: `assert len(backend.find("alice@example.com")) == 1`.
- For templating, use Jinja2 in `EmailService` methods before passing to the backend.
