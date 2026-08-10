from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any, Protocol, Sequence

import websocket


class CdpClient(Protocol):
    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...


class CdpError(RuntimeError):
    pass


class CdpWebSocketClient:
    """Small synchronous CDP transport for installed Chrome."""

    def __init__(self, websocket_url: str, timeout_seconds: float = 30) -> None:
        self._socket = websocket.create_connection(
            websocket_url,
            timeout=timeout_seconds,
            origin="http://localhost",
        )
        self._next_id = 0
        self._lock = threading.Lock()

    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            command_id = self._next_id
            message: dict[str, Any] = {
                "id": command_id,
                "method": method,
                "params": params or {},
            }
            if session_id:
                message["sessionId"] = session_id
            self._socket.send(json.dumps(message, ensure_ascii=False))
            while True:
                response = json.loads(self._socket.recv())
                if response.get("id") != command_id:
                    continue
                if "error" in response:
                    raise CdpError(f"{method} failed: {response['error']}")
                return response.get("result", {})

    def close(self) -> None:
        self._socket.close()


class ChromePageController:
    """DOM and upload primitives implemented directly over Chrome DevTools."""

    def __init__(self, cdp: CdpClient, session_id: str) -> None:
        self._cdp = cdp
        self._session_id = session_id

    @classmethod
    def attach(cls, cdp: CdpClient) -> ChromePageController:
        targets = cdp.command("Target.getTargets").get("targetInfos", [])
        page_target = next(
            (target for target in targets if target.get("type") == "page"),
            None,
        )
        if page_target is None:
            raise CdpError("Chrome has no page target to attach")
        attached = cdp.command(
            "Target.attachToTarget",
            {"targetId": page_target["targetId"], "flatten": True},
        )
        return cls(cdp, attached["sessionId"])

    def navigate(self, url: str) -> None:
        self._cdp.command("Page.navigate", {"url": url}, self._session_id)

    def evaluate(self, expression: str) -> Any:
        result = self._cdp.command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            self._session_id,
        )
        if result.get("exceptionDetails"):
            raise CdpError(f"browser expression failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def exists(self, selector: str) -> bool:
        return bool(self.evaluate(f"Boolean(document.querySelector({json.dumps(selector)}))"))

    def wait_for_selector(self, selector: str, timeout_seconds: float = 30) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.exists(selector):
                return
            time.sleep(0.25)
        raise CdpError(f"selector did not appear within {timeout_seconds:g}s: {selector}")

    def click(self, selector: str) -> None:
        expression = f"""
(() => {{
  const element = document.querySelector({json.dumps(selector)});
  if (!element) throw new Error('selector not found');
  element.scrollIntoView({{block: 'center'}});
  element.click();
  return true;
}})()
""".strip()
        self.evaluate(expression)

    def fill(self, selector: str, value: str) -> None:
        expression = f"""
(() => {{
  const element = document.querySelector({json.dumps(selector)});
  if (!element) throw new Error('selector not found');
  const value = {json.dumps(value, ensure_ascii=False)};
  if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {{
    const prototype = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
    setter.call(element, value);
  }} else {{
    element.textContent = value;
  }}
  element.dispatchEvent(new Event('input', {{bubbles: true}}));
  element.dispatchEvent(new Event('change', {{bubbles: true}}));
  return true;
}})()
""".strip()
        self.evaluate(expression)

    def fill_tags(
        self,
        selector: str,
        tags: Sequence[str],
        chip_selectors: Sequence[str],
    ) -> bool:
        expression = f"""
(() => {{
  const element = document.querySelector({json.dumps(selector)});
  if (!(element instanceof HTMLInputElement)) throw new Error('tag input not found');
  const tags = {json.dumps(list(tags), ensure_ascii=False)};
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  for (const tag of tags) {{
    setter.call(element, tag);
    element.dispatchEvent(new Event('input', {{bubbles: true}}));
    element.dispatchEvent(new Event('change', {{bubbles: true}}));
    element.dispatchEvent(new KeyboardEvent('keydown', {{
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
    }}));
    element.dispatchEvent(new KeyboardEvent('keyup', {{
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
    }}));
  }}
  return true;
}})()
""".strip()
        self.evaluate(expression)
        verification = f"""
(() => {{
  const element = document.querySelector({json.dumps(selector)});
  if (!(element instanceof HTMLInputElement)) return false;
  const scope = element.closest('[class*="tag" i]');
  if (!scope) return false;
  const normalize = value => value
    .replace(/[×✕]/g, '')
    .replace(/删除/g, '')
    .replace(/\\s+/g, ' ')
    .trim();
  const chipSelectors = {json.dumps(list(chip_selectors), ensure_ascii=False)};
  const chips = chipSelectors.flatMap(selector => [...scope.querySelectorAll(selector)])
    .map(node => normalize(node.getAttribute('data-tag') || node.textContent || ''));
  return {json.dumps(list(tags), ensure_ascii=False)}
    .map(normalize)
    .every(tag => chips.includes(tag));
}})()
""".strip()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if bool(self.evaluate(verification)):
                return True
            time.sleep(0.2)
        return False

    def set_files(self, selector: str, paths: Sequence[Path]) -> None:
        root = self._cdp.command(
            "DOM.getDocument",
            {"depth": 1, "pierce": True},
            self._session_id,
        )
        query = self._cdp.command(
            "DOM.querySelector",
            {"nodeId": root["root"]["nodeId"], "selector": selector},
            self._session_id,
        )
        node_id = int(query.get("nodeId", 0))
        if node_id <= 0:
            raise CdpError(f"file input selector not found: {selector}")
        self._cdp.command(
            "DOM.setFileInputFiles",
            {"nodeId": node_id, "files": [str(path.resolve()) for path in paths]},
            self._session_id,
        )

    def screenshot(self, output_path: Path) -> Path:
        result = self._cdp.command(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
            self._session_id,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(result["data"]))
        return output_path
