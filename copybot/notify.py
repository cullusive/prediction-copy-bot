"""Alerts. Discord webhook when configured, otherwise the console."""

from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def send(self, text: str) -> None:
        print(text, flush=True)


class DiscordWebhook(Notifier):
    """Posts to a Discord channel webhook (Server Settings > Integrations > Webhooks).
    The URL is a secret: keep it in the COPYBOT_DISCORD_WEBHOOK environment
    variable, never in the code or the repo."""

    def __init__(self, url: str):
        self.url = url

    def send(self, text: str) -> None:
        super().send(text)
        body = json.dumps({"content": text[:1990]}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "prediction-copy-bot/0.1"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as exc:  # alerts must never stop trading logic
            log.warning("discord notify failed: %s", exc)


def from_env() -> Notifier:
    url = os.environ.get("COPYBOT_DISCORD_WEBHOOK")
    return DiscordWebhook(url) if url else Notifier()
