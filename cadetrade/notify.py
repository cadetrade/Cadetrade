"""La voce: Telegram. Se Telegram non risponde la sveglia non si ferma, lo scrive nel log."""
import json
import logging
import urllib.request

log = logging.getLogger("telegram")


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token, self.chat_id = token, chat_id
        self.sent: list[str] = []   # utile nei test e in console

    def send(self, text: str) -> None:
        self.sent.append(text)
        if not (self.token and self.chat_id):
            log.info("[console] %s", text.replace("\n", " | "))
            return
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            log.error("Telegram non risponde (%s): messaggio solo nel log", type(e).__name__)

    def alarm(self, text: str, run_id: str) -> None:
        self.send(f"⚠️ {text}\n· run {run_id}")
