#!/usr/bin/env python3
"""Print an HTML page to an A4 PDF through the Firefox snap's geckodriver (the only browser on the box).

The page may be an Artifact-style fragment (no doctype/head): it is wrapped in a full document, forced to the
light theme, and printed with backgrounds. Web fonts load from the network, so give them a few seconds.

Usage: python3 ops/scripts/print-pdf.py report/headless-dm-logbook.html [out.pdf]
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = 4455


def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))


def main():
    src = os.path.abspath(sys.argv[1])
    out = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.splitext(src)[0] + ".pdf"
    # The snap's Firefox can read files under $HOME but not the host /tmp.
    copy = os.path.join(os.path.dirname(src), ".print-copy.html")
    body = open(src, encoding="utf-8").read()
    if "<!doctype" not in body[:200].lower():
        body = ('<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">' + body + "</html>")
    open(copy, "w", encoding="utf-8").write(body)
    profiles = os.path.expanduser("~/snap/firefox/common/geckodriver-profiles")
    os.makedirs(profiles, exist_ok=True)
    driver = subprocess.Popen(["/snap/bin/geckodriver", "--port", str(PORT), "--profile-root", profiles],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        sid = call("POST", "/session", {"capabilities": {"alwaysMatch": {
            "moz:firefoxOptions": {"args": ["-headless"]}}}})["value"]["sessionId"]
        call("POST", f"/session/{sid}/url", {"url": "file://" + copy})
        time.sleep(4)
        pdf = call("POST", f"/session/{sid}/print", {
            "background": True, "shrinkToFit": True, "page": {"width": 21.0, "height": 29.7},
            "margin": {"top": 1.2, "bottom": 1.2, "left": 1.1, "right": 1.1}})["value"]
        open(out, "wb").write(base64.b64decode(pdf))
        call("DELETE", f"/session/{sid}")
    finally:
        driver.terminate()
        os.remove(copy)
    print(f"wrote {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    main()
