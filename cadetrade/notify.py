"""La voce: Telegram. Se Telegram non risponde la sveglia non si ferma, lo scrive nel log."""
import json
import logging
import urllib.request
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("telegram")


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token, self.chat_id = token, chat_id
        self.sent: list[str] = []   # utile nei test e in console
        self.feed = deque(maxlen=60)  # (ora, testo) per il pannello dal vivo

    def _resolve_chat_id(self) -> str:
        """Senza chat id lo ricava dall'ultimo messaggio scritto al bot (basta avergli scritto "ciao")."""
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{self.token}/getUpdates", timeout=10) as r:
                updates = json.loads(r.read()).get("result", [])
            for u in reversed(updates):
                chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat", {})
                if chat.get("id"):
                    self.chat_id = str(chat["id"])
                    log.info("chat id Telegram trovato: %s", self.chat_id)
                    break
        except Exception as e:
            log.error("chat id Telegram non trovato (%s): scrivi 'ciao' al bot", type(e).__name__)
        return self.chat_id

    def send(self, text: str) -> None:
        self.sent.append(text)
        self.feed.append((datetime.now(timezone.utc).isoformat(), text))
        if self.token and not self.chat_id:
            self._resolve_chat_id()
        if not (self.token and self.chat_id):
            log.info("[console] %s", text.replace("\n", " | "))
            return
        log.info("%s", text.replace("\n", " | ")[:600])   # copia nel log: si legge anche dal pannello Hostinger
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            log.error("Telegram non risponde (%s): messaggio solo nel log", type(e).__name__)

    def alarm(self, text: str, run_id: str) -> None:
        self.send(f"⚠️ {text}\n· run {run_id}")
